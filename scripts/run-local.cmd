@echo off
setlocal
cd /d "%~dp0.."

rem Portable tools installed by scripts\setup-windows.ps1.
set "PATH=%CD%\tools;%PATH%"

rem Winget may update the user PATH, which an already-open terminal does not
rem see. Add its links and package folders directly so this window works too.
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links" set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*") do (
    for /d %%B in ("%%D\ffmpeg-*_build") do (
        if exist "%%B\bin\ffmpeg.exe" set "PATH=%%B\bin;%PATH%"
    )
)
for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\aria2.aria2_*") do (
    for /d %%B in ("%%D\aria2-*win*") do (
        if exist "%%B\aria2c.exe" set "PATH=%%B;%PATH%"
    )
)

rem This run is the primary PC runner. On startup it cancels any active
rem bot.yml fallback run; job.yml workers keep running. On exit it starts one
rem on-demand fallback run. With REMOTE_JOBS=1, mirror jobs run on job.yml.
set "LOCAL_MODE=1"

rem Keep the tracked .state/telegram.json file for GitHub Actions only.
if not defined STATE_FILE set "STATE_FILE=.state\local-telegram.json"

rem Stop without handing over: scripts\run-local.cmd nohandoff
if /I "%~1"=="nohandoff" set "AUTO_HANDOFF=0"

if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found.
    echo Run: powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
    exit /b 1
)

echo Starting the mirror bot on this PC.
echo Press Ctrl+C to stop it and start the GitHub Actions fallback.
echo.
".venv\Scripts\python.exe" -m bot
