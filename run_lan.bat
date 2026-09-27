@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" goto setup_required
if not exist "python-kokoro\.venv\Scripts\python.exe" goto setup_required
goto setup_complete

:setup_required
echo [RUN_LAN.BAT] Required environment missing. Running setup.bat first...
call setup.bat
if errorlevel 1 exit /b %errorlevel%

:setup_complete
if not exist ".venv\Scripts\activate.bat" (
    echo [RUN_LAN.BAT] Main virtual environment is still missing.
    exit /b 1
)
if not exist "python-kokoro\.venv\Scripts\python.exe" (
    echo [RUN_LAN.BAT] Required Kokoro environment is still missing.
    exit /b 1
)

rem run.ps1 selects LAN binding unless -Local is supplied by run_default.bat.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %errorlevel%
