@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0python-kokoro\setup.ps1" %*
exit /b %errorlevel%
