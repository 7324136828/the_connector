@echo off
setlocal
cd /d "%~dp0"

if defined VIRTUAL_ENV goto active
if defined CONDA_PREFIX goto active
if exist ".venv\Scripts\python.exe" goto local
where python >nul 2>nul
if errorlevel 1 goto pythonlauncher

:active
python build.py %*
exit /b %ERRORLEVEL%

:local
".venv\Scripts\python.exe" build.py %*
exit /b %ERRORLEVEL%

:pythonlauncher
py -3 build.py %*
exit /b %ERRORLEVEL%
