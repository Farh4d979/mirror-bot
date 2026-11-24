# Enable split mode: PC polls Telegram, GitHub Actions runs the jobs.
#
# Run from PowerShell in the project directory:
#   powershell -ExecutionPolicy Bypass -File scripts\configure-remote.ps1
#
# The script generates a Fernet key once, stores it in .env (gitignored), and
# uploads the same value as the REMOTE_JOB_KEY repository secret. The key is
# never printed. Re-run with -Rotate to replace it.

param(
    [string]$EnvFile,
    [string]$Repo,
    [switch]$Rotate,
    [switch]$NoSecret
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) {
    $EnvFile = Join-Path $Root ".env"
}
$Template = Join-Path $Root ".env.example"

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

function New-FernetKey {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return [System.Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')
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

$existing = Get-EnvValue $lines "REMOTE_JOB_KEY"
if ($Rotate -or $existing -notmatch '^[A-Za-z0-9_-]{43}=$') {
    $key = New-FernetKey
    Set-EnvValue $lines "REMOTE_JOB_KEY" $key
    Write-Host "Generated a new REMOTE_JOB_KEY." -ForegroundColor Green
}
else {
    $key = $existing
    Write-Host "Keeping the existing REMOTE_JOB_KEY (use -Rotate to replace it)."
}
Set-EnvValue $lines "REMOTE_JOBS" "1"

$parent = Split-Path -Parent $EnvFile
if ($parent -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent | Out-Null
}
$utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
[System.IO.File]::WriteAllLines($EnvFile, $lines, $utf8)

if (-not $NoSecret) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'GitHub CLI (gh) is not installed. Install it from https://cli.github.com/ and run "gh auth login".'
    }

    if (-not $Repo) {
        $repoJson = gh repo view --json nameWithOwner
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not detect the repository. Run "gh auth login" or pass -Repo owner/name.'
        }
        $Repo = ($repoJson | ConvertFrom-Json).nameWithOwner
    }

    $key | gh secret set REMOTE_JOB_KEY --repo $Repo
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload REMOTE_JOB_KEY to $Repo. The key is saved in $EnvFile; fix gh access and re-run this script."
    }
    Write-Host "Uploaded REMOTE_JOB_KEY to $Repo." -ForegroundColor Green
}

Write-Host ""
Write-Host "Saved $EnvFile." -ForegroundColor Green
Write-Host "  REMOTE_JOBS    = 1"
Write-Host "  REMOTE_JOB_KEY = (set, hidden)"
Write-Host ""
Write-Host "Next: restart scripts\run-local.cmd or the MirrorBot task." -ForegroundColor Cyan
