@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [RUN.BAT] Virtual environment not found. Running setup.bat first...
    call setup.bat
)

echo ========================================================
echo Starting The Connector Full-Stack Services
echo Backend:  http://localhost:8301 (API ^& Docs: /docs)
echo Frontend: http://localhost:5173
echo ========================================================

:: Start backend in background or separate window
start "The Connector Backend API" cmd /k "call run_backend.bat --reload"

:: Start frontend in this terminal
cd frontend
call npm run dev

echo [RUN.BAT] Shutting down...
