# Register the mirror bot to start at Windows logon.
#
# Run once from PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\install-startup-task.ps1
#
# Remove it with:
#   Unregister-ScheduledTask -TaskName MirrorBot -Confirm:$false

param(
    [string]$TaskName = "MirrorBot",
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run-local.cmd"

if (-not (Test-Path $Runner)) {
    throw "Runner not found: $Runner"
}
if (-not (Test-Path (Join-Path $Root ".env"))) {
    throw "Create .env first (copy .env.example) and fill in TELEGRAM_BOT_TOKEN."
}

$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$Runner`"" `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$Settings.DisallowStartIfOnBatteries = $false
$Settings.StopIfGoingOnBatteries = $false

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Run the Telegram mirror bot on this PC and hand over to GitHub Actions on exit." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "It starts at the next logon."
if ($Start) {
    Write-Host "Starting it now..."
    Start-ScheduledTask -TaskName $TaskName
}
