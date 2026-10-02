@echo off
setlocal
if defined COUNT_MESSAGE_PYTHON (
  "%COUNT_MESSAGE_PYTHON%" "%~dp0run.py" %*
) else (
  py -3 "%~dp0run.py" %*
)
exit /b %errorlevel%
