# Point Telegram at the Cloudflare Worker, or give the bot back to the PC.
#
# Cut over only after scripts\configure-cloudflare.ps1 reports a ready Worker:
#   powershell -ExecutionPolicy Bypass -File scripts\cloudflare-webhook.ps1 -Mode on
#
# Go back to PC long polling:
#   powershell -ExecutionPolicy Bypass -File scripts\cloudflare-webhook.ps1 -Mode off -StartPcBot
#
# Telegram allows only one update consumer. `-Mode on` stops and disables the
# MirrorBot scheduled task unless -KeepPcBot is passed. Secrets and the bot
# token are never printed.

param(
    [ValidateSet("on", "off", "status")]
    [string]$Mode = "status",
    [string]$EnvFile,
    [switch]$KeepPcBot,
    [switch]$StartPcBot
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) {
    $EnvFile = Join-Path $Root ".env"
}
$TaskName = "MirrorBot"

function Get-EnvValue([string]$Key) {
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*=(.*)$'
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        if ($line -match $pattern) {
            return $Matches[1].Trim()
        }
    }
    return ""
}

function Invoke-TelegramApi([string]$Method, [hashtable]$Body) {
    $json = $Body | ConvertTo-Json -Depth 10 -Compress
    try {
        return Invoke-RestMethod -Method Post `
            -Uri "https://api.telegram.org/bot$botToken/$Method" `
            -ContentType "application/json; charset=utf-8" `
            -Body $json -TimeoutSec 30
    }
    catch {
        $message = "Telegram $Method failed."
        if ($_.ErrorDetails.Message) {
            try {
                $parsed = $_.ErrorDetails.Message | ConvertFrom-Json
                if ($parsed.description) {
                    $message = "Telegram ${Method} failed: $($parsed.description)"
                }
            }
            catch {
                # Keep the generic message; the raw error may contain the URL.
            }
        }
        throw $message
    }
}

if (-not (Test-Path $EnvFile)) {
    throw "$EnvFile does not exist. Run scripts\configure-env.ps1 first."
}

$botToken = Get-EnvValue "TELEGRAM_BOT_TOKEN"
if (-not $botToken -or $botToken -match '^123456789:replace-me$') {
    throw "TELEGRAM_BOT_TOKEN is not configured in $EnvFile."
}
$workerUrl = (Get-EnvValue "CLOUDFLARE_WORKER_URL").TrimEnd('/')
$webhookSecret = Get-EnvValue "TELEGRAM_WEBHOOK_SECRET"

if ($Mode -eq "status") {
    $info = Invoke-TelegramApi "getWebhookInfo" @{}
    $result = $info.result
    Write-Host ("Webhook: {0}" -f ($(if ($result.url) { $result.url } else { "(none)" })))
    Write-Host ("Pending updates: {0}" -f $result.pending_update_count)
    if ($result.last_error_message) {
        Write-Host ("Last error: {0}" -f $result.last_error_message) -ForegroundColor Yellow
    }
    return
}

if ($Mode -eq "on") {
    if (-not $workerUrl) {
        throw "CLOUDFLARE_WORKER_URL is not set in $EnvFile. Run scripts\configure-cloudflare.ps1 first."
    }
    if ($webhookSecret -notmatch '^[A-Za-z0-9_-]{1,256}$') {
        throw "TELEGRAM_WEBHOOK_SECRET is missing or invalid in $EnvFile. Re-run scripts\configure-cloudflare.ps1."
    }

    try {
        $health = Invoke-RestMethod -Uri "$workerUrl/health" -TimeoutSec 15
    }
    catch {
        throw "The Worker at $workerUrl is not reachable. Check the deploy and try again."
    }
    if (-not $health.ok) {
        $missing = if ($health.missing) { $health.missing -join ", " } else { "unknown" }
        throw "The Worker is not ready (missing: $missing). Re-run scripts\configure-cloudflare.ps1."
    }

    $pcWasRunning = $false
    $pcChanged = $false
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        if ($KeepPcBot) {
            if ($task.State -eq "Running") {
                Write-Host "Warning: $TaskName is still running. Its long poller will get 409 Conflict while the webhook is active." -ForegroundColor Yellow
            }
        }
        else {
            $pcWasRunning = $task.State -eq "Running"
            if ($pcWasRunning) {
                Stop-ScheduledTask -TaskName $TaskName
                Write-Host "Stopped the $TaskName scheduled task so Telegram has one update consumer." -ForegroundColor Green
            }
            if ($task.State -ne "Disabled") {
                Disable-ScheduledTask -TaskName $TaskName | Out-Null
                $pcChanged = $true
                Write-Host "Disabled $TaskName at logon. Re-enable it with -Mode off." -ForegroundColor Green
            }
            Start-Sleep -Seconds 2
        }
    }

    try {
        $response = Invoke-TelegramApi "setWebhook" @{
            url = "$workerUrl/telegram"
            secret_token = $webhookSecret
            allowed_updates = @("message", "callback_query")
            drop_pending_updates = $true
            max_connections = 10
        }
        if (-not $response.ok) {
            throw "Telegram rejected the webhook."
        }
    }
    catch {
        if ($pcChanged) {
            Enable-ScheduledTask -TaskName $TaskName | Out-Null
            if ($pcWasRunning) {
                Start-ScheduledTask -TaskName $TaskName
            }
            Write-Host "Restored the PC poller because the webhook could not be set." -ForegroundColor Yellow
        }
        throw
    }
    Write-Host "Telegram is now using the Cloudflare Worker:" -ForegroundColor Green
    Write-Host "  $workerUrl/telegram"
    Write-Host ("  description: {0}" -f $response.description)
    Write-Host ""
    Write-Host "Send /id and then a small /gf link to verify. Use -Mode status to inspect the webhook."
    return
}

# Mode off
$response = Invoke-TelegramApi "deleteWebhook" @{
    drop_pending_updates = $false
}
if (-not $response.ok) {
    throw "Telegram rejected the deleteWebhook request."
}
Write-Host ("Telegram webhook removed: {0}" -f $response.description) -ForegroundColor Green

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "The $TaskName scheduled task was not found. Start the PC poller manually if needed." -ForegroundColor Yellow
}
else {
    if ($task.State -eq "Disabled") {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host "Re-enabled the $TaskName scheduled task at logon." -ForegroundColor Green
    }
    if ($StartPcBot) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Started the $TaskName scheduled task." -ForegroundColor Green
    }
    else {
        Write-Host "Next: start the PC poller with:" -ForegroundColor Cyan
        Write-Host "  Start-ScheduledTask -TaskName $TaskName"
    }
}
