@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" goto setup_required
if not exist "python-kokoro\.venv\Scripts\python.exe" goto setup_required
goto setup_complete

:setup_required
echo [RUN.BAT] Required environment missing. Running setup.bat first...
call setup.bat
if errorlevel 1 exit /b %errorlevel%

:setup_complete
if not exist ".venv\Scripts\activate.bat" (
    echo [RUN.BAT] Main virtual environment is still missing.
    exit /b 1
)
if not exist "python-kokoro\.venv\Scripts\python.exe" (
    echo [RUN.BAT] Required Kokoro environment is still missing.
    exit /b 1
)

echo ========================================================
echo Starting The Connector Full-Stack Services
echo Backend:  http://localhost:8301 (API ^& Docs: /docs)
echo Kokoro:  http://localhost:8302 (required speech backend)
echo Frontend: http://localhost:5173
echo ========================================================

:: Keep all three services in this console. The PowerShell supervisor also
:: tears down each service's child processes when this launcher stops.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
set "RUN_EXIT_CODE=%errorlevel%"
exit /b %RUN_EXIT_CODE%
