# Deploy the Cloudflare Worker controller.
#
# Run after `npx wrangler login`:
#   powershell -ExecutionPolicy Bypass -File scripts\configure-cloudflare.ps1
#
# This script reads the existing .env, creates cloudflare\.dev.vars (gitignored),
# deploys the Worker, uploads its secrets with `wrangler secret bulk`, and stores
# the Worker URL in .env. It never prints a secret value.
#
# It does NOT point Telegram at the Worker. Do that only after /health says the
# Worker is ready:
#   powershell -ExecutionPolicy Bypass -File scripts\cloudflare-webhook.ps1 -Mode on

param(
    [string]$EnvFile,
    [string]$WorkerUrl,
    [string]$Repo,
    [switch]$SkipDeploy,
    [switch]$SkipSecrets,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$CloudflareDir = Join-Path $Root "cloudflare"
if (-not $EnvFile) {
    $EnvFile = Join-Path $Root ".env"
}
$Template = Join-Path $Root ".env.example"
$DevVars = Join-Path $CloudflareDir ".dev.vars"

function Set-EnvValue(
    [System.Collections.Generic.List[string]]$Lines,
    [string]$Key,
    [string]$Value
) {
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*='
    $found = $false
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match $pattern) {
            $Lines[$i] = "$Key=$Value"
            $found = $true
        }
    }
    if (-not $found) {
        $Lines.Add("$Key=$Value")
    }
}

function Get-EnvValue(
    [System.Collections.Generic.List[string]]$Lines,
    [string]$Key
) {
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*=(.*)$'
    foreach ($line in $Lines) {
        if ($line -match $pattern) {
            return $Matches[1].Trim()
        }
    }
    return ""
}

function New-WebhookSecret {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return [System.Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
}

function Assert-FernetKey([string]$Key) {
    if ($Key -notmatch '^[A-Za-z0-9_-]{43}=$') {
        throw "REMOTE_JOB_KEY in $EnvFile is not a valid Fernet key. Run scripts\configure-remote.ps1 first."
    }
}

if (-not (Test-Path $EnvFile)) {
    if (Test-Path $Template) {
        Copy-Item $Template $EnvFile
        Write-Host "Created $EnvFile from .env.example." -ForegroundColor Green
    }
    else {
        throw "Neither $EnvFile nor $Template exists."
    }
}

$source = [System.IO.File]::ReadAllLines($EnvFile)
$lines = [System.Collections.Generic.List[string]]::new()
foreach ($line in $source) {
    $lines.Add($line)
}

$botToken = Get-EnvValue $lines "TELEGRAM_BOT_TOKEN"
if (-not $botToken -or $botToken -match '^123456789:replace-me$') {
    throw "TELEGRAM_BOT_TOKEN is not configured in $EnvFile. Run scripts\configure-env.ps1 first."
}
$remoteKey = Get-EnvValue $lines "REMOTE_JOB_KEY"
Assert-FernetKey $remoteKey

$allowedIds = Get-EnvValue $lines "ALLOWED_USER_IDS"
$gofileToken = Get-EnvValue $lines "GOFILE_TOKEN"
$pixeldrainKey = Get-EnvValue $lines "PIXELDRAIN_API_KEY"

$webhookSecret = Get-EnvValue $lines "TELEGRAM_WEBHOOK_SECRET"
if ($webhookSecret -notmatch '^[A-Za-z0-9_-]{16,256}$') {
    $webhookSecret = New-WebhookSecret
    Set-EnvValue $lines "TELEGRAM_WEBHOOK_SECRET" $webhookSecret
    Write-Host "Generated a new Telegram webhook secret." -ForegroundColor Green
}
else {
    Write-Host "Keeping the existing Telegram webhook secret."
}

$githubToken = $env:GITHUB_TOKEN
if (-not $githubToken) {
    $githubToken = Get-EnvValue $lines "GITHUB_TOKEN"
}
if (-not $githubToken) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'GitHub CLI (gh) is not installed. Install it from https://cli.github.com/, run "gh auth login", or set GITHUB_TOKEN.'
    }
    $githubToken = (gh auth token 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $githubToken) {
        throw 'Could not read a GitHub token. Run "gh auth login", set GITHUB_TOKEN, or add GITHUB_TOKEN to .env.'
    }
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js 20+ is required to build the Worker. Install it from https://nodejs.org/ and re-run this script."
}
$nodeVersion = (node --version).TrimStart('v')
$nodeMajor = [int]($nodeVersion.Split('.')[0])
if ($nodeMajor -lt 20) {
    throw "Node.js 20+ is required; found $nodeVersion."
}

if (-not $SkipDeploy -and -not (Test-Path (Join-Path $CloudflareDir "node_modules"))) {
    if ($SkipInstall) {
        throw "cloudflare\node_modules is missing. Run npm ci in cloudflare\ first."
    }
    Write-Host "Installing Worker dependencies..." -ForegroundColor Cyan
    Push-Location $CloudflareDir
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed." }
    }
    finally {
        Pop-Location
    }
}

Push-Location $CloudflareDir
try {
    if (-not $SkipDeploy) {
        Write-Host "Checking Cloudflare authentication..." -ForegroundColor Cyan
        & npx wrangler whoami 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw 'Wrangler is not authenticated. Run "cd cloudflare; npx wrangler login" and re-run this script.'
        }

        Write-Host "Deploying the Worker..." -ForegroundColor Cyan
        $deployOutput = & npx wrangler deploy 2>&1 | Out-String
        Write-Host $deployOutput
        if ($LASTEXITCODE -ne 0) {
            throw "wrangler deploy failed."
        }
        if (-not $WorkerUrl) {
            $match = [regex]::Match($deployOutput, 'https://[A-Za-z0-9.-]+\.workers\.dev')
            if ($match.Success) {
                $WorkerUrl = $match.Value
            }
        }
    }

    if (-not $WorkerUrl) {
        $WorkerUrl = Get-EnvValue $lines "CLOUDFLARE_WORKER_URL"
    }
    if (-not $WorkerUrl) {
        throw "Could not determine the Worker URL. Copy it from the deploy output and re-run with -WorkerUrl https://...workers.dev"
    }
    $WorkerUrl = $WorkerUrl.TrimEnd('/')

    if (-not $SkipSecrets) {
        $secretLines = [System.Collections.Generic.List[string]]::new()
        $secretLines.Add("TELEGRAM_BOT_TOKEN=$botToken")
        $secretLines.Add("TELEGRAM_WEBHOOK_SECRET=$webhookSecret")
        $secretLines.Add("GITHUB_TOKEN=$githubToken")
        $secretLines.Add("REMOTE_JOB_KEY=$remoteKey")
        if ($allowedIds) { $secretLines.Add("ALLOWED_USER_IDS=$allowedIds") }
        if ($gofileToken) { $secretLines.Add("GOFILE_TOKEN=$gofileToken") }
        if ($pixeldrainKey) { $secretLines.Add("PIXELDRAIN_API_KEY=$pixeldrainKey") }
        $utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
        [System.IO.File]::WriteAllLines($DevVars, $secretLines, $utf8)
        Write-Host "Uploading Worker secrets..." -ForegroundColor Cyan
        & npx wrangler secret bulk $DevVars
        if ($LASTEXITCODE -ne 0) {
            throw "wrangler secret bulk failed. The values remain in $DevVars (gitignored)."
        }
    }
}
finally {
    Pop-Location
}

Set-EnvValue $lines "CLOUDFLARE_WORKER_URL" $WorkerUrl
$utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
[System.IO.File]::WriteAllLines($EnvFile, $lines, $utf8)

Write-Host ""
Write-Host "Worker URL: $WorkerUrl" -ForegroundColor Green
$health = $null
for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "$WorkerUrl/health" -TimeoutSec 15
        break
    }
    catch {
        if ($attempt -eq 5) {
            Write-Host "Could not read /health: $($_.Exception.Message)" -ForegroundColor Yellow
        }
        else {
            Start-Sleep -Seconds 2
        }
    }
}
if ($health) {
    Write-Host ("Health: ok={0} ready={1} active_jobs={2}" -f $health.ok, $health.ready, $health.active_jobs)
    if ($health.missing -and $health.missing.Count -gt 0) {
        Write-Host ("Missing secrets: {0}" -f ($health.missing -join ", ")) -ForegroundColor Yellow
    }
    if ($health.invalid -and $health.invalid.Count -gt 0) {
        Write-Host ("Invalid secrets: {0}" -f ($health.invalid -join ", ")) -ForegroundColor Yellow
    }
    if (-not $health.ok) {
        Write-Host "The Worker is deployed but not ready. Fix the names above, then re-run this script." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Next: cut Telegram over to the Worker (this stops and disables the PC poller):" -ForegroundColor Cyan
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\cloudflare-webhook.ps1 -Mode on"
Write-Host ""
Write-Host "To go back to the PC poller:" -ForegroundColor Cyan
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\cloudflare-webhook.ps1 -Mode off -StartPcBot"
