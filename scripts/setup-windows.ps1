# Install the tools needed to run the mirror bot on Windows.
#
# Run from PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
#
# This installs aria2 and ffmpeg with winget, downloads megatools and
# payload-dumper-go into tools\, creates .venv, and installs requirements.txt.
# It does not start the bot or change .env.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Tools = Join-Path $Root "tools"
New-Item -ItemType Directory -Force -Path $Tools | Out-Null

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Ensure-WingetPackage {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Command
    )
    if (Get-Command $Command -ErrorAction SilentlyContinue) {
        Write-Host "$Command is already installed."
        return
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is required to install $Command. Install App Installer from the Microsoft Store."
    }
    Write-Host "Installing $Id..."
    winget install --id $Id -e --accept-source-agreements --accept-package-agreements --silent
    Refresh-Path
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Id was installed but $Command is not on PATH. Open a new terminal and run this script again."
    }
}

Refresh-Path

Ensure-WingetPackage -Id "aria2.aria2" -Command "aria2c"
Ensure-WingetPackage -Id "Gyan.FFmpeg.Essentials" -Command "ffmpeg"

function Download-File {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Path
    )
    Write-Host "Downloading $Uri"
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Path
}

# megatools: official Windows build, same version/hash as the Scoop manifest.
$MegaVersion = "1.11.5.20250706"
$MegaUrl = "https://xff.cz/megatools/builds/builds/megatools-$MegaVersion-win64.zip"
$MegaZip = Join-Path $env:TEMP "megatools-$MegaVersion.zip"
$MegaHash = "bd1269e50d9c45e369c14e287c1754c9506e1b11efcc0cd4bb95d460c9d782b5"

Download-File -Uri $MegaUrl -Path $MegaZip
$actualHash = (Get-FileHash -Algorithm SHA256 -Path $MegaZip).Hash.ToLowerInvariant()
if ($actualHash -ne $MegaHash) {
    throw "megatools checksum mismatch: $actualHash"
}

$MegaTemp = Join-Path $env:TEMP ("megatools-" + [guid]::NewGuid().ToString("N"))
Expand-Archive -Path $MegaZip -DestinationPath $MegaTemp -Force
$MegaSource = Get-ChildItem -Path $MegaTemp -Directory | Select-Object -First 1
if ($null -ne $MegaSource) {
    Copy-Item -Path (Join-Path $MegaSource.FullName "*") -Destination $Tools -Recurse -Force
} else {
    Copy-Item -Path (Join-Path $MegaTemp "*") -Destination $Tools -Recurse -Force
}
Remove-Item -Path $MegaTemp -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path $MegaZip -Force -ErrorAction SilentlyContinue

if (-not (Test-Path (Join-Path $Tools "megatools.exe"))) {
    throw "megatools.exe was not extracted into tools\."
}

# payload-dumper-go: Windows x64 build.
$DumperVersion = "2.1.0"
$DumperUrl = "https://github.com/ssut/payload-dumper-go/releases/download/$DumperVersion/payload-dumper-go_${DumperVersion}_windows_amd64.tar.gz"
$DumperTar = Join-Path $env:TEMP "payload-dumper-go-$DumperVersion.tar.gz"
Download-File -Uri $DumperUrl -Path $DumperTar
& tar -xzf $DumperTar -C $Tools payload-dumper-go.exe
Remove-Item -Path $DumperTar -Force -ErrorAction SilentlyContinue

if (-not (Test-Path (Join-Path $Tools "payload-dumper-go.exe"))) {
    throw "payload-dumper-go.exe was not extracted into tools\."
}

# Python environment.
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Creating .venv..."
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $PyLauncher) {
        & py -3.12 -m venv .venv
        if ($LASTEXITCODE -ne 0) {
            & python -m venv .venv
        }
    } else {
        & python -m venv .venv
    }
}
if (-not (Test-Path $Python)) {
    throw "Could not create .venv. Install Python 3.12 from https://www.python.org/downloads/ and run this script again."
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt

# .env is created once and never overwritten.
if (-not (Test-Path (Join-Path $Root ".env"))) {
    Copy-Item -Path (Join-Path $Root ".env.example") -Destination (Join-Path $Root ".env")
    Write-Host "Created .env from .env.example." -ForegroundColor Yellow
}

# Final checks.
$missing = @()
foreach ($command in @("aria2c", "ffmpeg", "gh")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        $missing += $command
    }
}
if ($missing.Count -gt 0) {
    Write-Warning "Missing commands: $($missing -join ', ')"
}
if (-not (Test-Path (Join-Path $Tools "megatools.exe"))) { $missing += "megatools.exe" }
if (-not (Test-Path (Join-Path $Tools "payload-dumper-go.exe"))) { $missing += "payload-dumper-go.exe" }

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Edit .env and set TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS."
Write-Host "     Optional: GOFILE_TOKEN, PIXELDRAIN_API_KEY."
Write-Host "  2. Run: gh auth login   (and check: gh auth status)"
Write-Host "  3. Start: scripts\run-local.cmd"
Write-Host "  4. Optional auto-start at login:"
Write-Host "     powershell -ExecutionPolicy Bypass -File scripts\install-startup-task.ps1"
