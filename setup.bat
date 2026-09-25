@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo [SETUP.BAT] Checking Python installation...
echo ========================================================
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not in PATH.
    echo Please install Python 3.10+ and re-run.
    pause
    exit /b 1
)

echo [SETUP.BAT] Dispatching setup.py...
python setup.py %*
if %errorlevel% neq 0 (
    echo [SETUP.BAT] Error: Setup failed with code %errorlevel%.
    pause
    exit /b %errorlevel%
)

echo [SETUP.BAT] Setup finished successfully.
pause
