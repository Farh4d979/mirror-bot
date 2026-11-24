# Fill in the local .env without printing secrets to the terminal.
#
# Run from PowerShell in the project directory:
#   powershell -ExecutionPolicy Bypass -File scripts\configure-env.ps1
#
# The bot token is read as a hidden prompt. The file is .env, which is
# gitignored. Re-run this script whenever you rotate the token or add owners.

param(
    [string]$EnvFile,
    [string]$Token,
    [string]$AllowedUserIds
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) {
    $EnvFile = Join-Path $Root ".env"
}
$Template = Join-Path $Root ".env.example"

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

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

if (-not $PSBoundParameters.ContainsKey("Token")) {
    $Token = Read-Secret "BotFather token (input is hidden)"
}
$Token = $Token.Trim()
if (-not $Token) {
    throw "The bot token cannot be empty."
}
if ($Token -match "replace-me" -or $Token -notmatch '^\d+:[A-Za-z0-9_-]{20,}$') {
    throw "That does not look like a BotFather token. It should look like 123456789:AA..."
}

if (-not $PSBoundParameters.ContainsKey("AllowedUserIds")) {
    Write-Host ""
    Write-Host "Your Telegram user ID is shown by @userinfobot." -ForegroundColor Cyan
    $AllowedUserIds = Read-Host "Allowed user ID(s), comma-separated (blank = anyone)"
}
$AllowedUserIds = $AllowedUserIds.Trim()
if ($AllowedUserIds) {
    $ids = ($AllowedUserIds -replace ";", ",").Split(",") |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
    foreach ($id in $ids) {
        if ($id -notmatch '^\d+$') {
            throw "'$id' is not a numeric Telegram user ID."
        }
    }
    $AllowedUserIds = $ids -join ","
}

if (Test-Path $EnvFile) {
    $source = [System.IO.File]::ReadAllLines($EnvFile)
}
elseif (Test-Path $Template) {
    $source = [System.IO.File]::ReadAllLines($Template)
    Write-Host "Created $EnvFile from .env.example." -ForegroundColor Green
}
else {
    throw "Neither $EnvFile nor $Template exists."
}

$lines = [System.Collections.Generic.List[string]]::new()
foreach ($line in $source) {
    $lines.Add($line)
}
Set-EnvValue $lines "TELEGRAM_BOT_TOKEN" $Token
Set-EnvValue $lines "ALLOWED_USER_IDS" $AllowedUserIds

$parent = Split-Path -Parent $EnvFile
if ($parent -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent | Out-Null
}
$utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
[System.IO.File]::WriteAllLines($EnvFile, $lines, $utf8)

Write-Host ""
Write-Host "Saved $EnvFile." -ForegroundColor Green
Write-Host "  TELEGRAM_BOT_TOKEN = (set, hidden)"
Write-Host "  ALLOWED_USER_IDS   = $(if ($AllowedUserIds) { $AllowedUserIds } else { '(empty)' })"
if (-not $AllowedUserIds) {
    Write-Host "Warning: anyone who finds the bot can use it." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Next: scripts\run-local.cmd" -ForegroundColor Cyan
