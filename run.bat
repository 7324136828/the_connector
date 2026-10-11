@echo off
setlocal
cd /d "%~dp0"
if defined VIRTUAL_ENV goto active
if defined CONDA_PREFIX goto active
if not exist ".venv\Scripts\python.exe" goto active
".venv\Scripts\python.exe" "%~dp0run.py" %*
exit /b %errorlevel%

:active
python "%~dp0run.py" %*
exit /b %errorlevel%
