@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0python-kokoro\run.ps1" -Foreground %*
exit /b %errorlevel%
