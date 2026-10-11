@echo off
rem Local launch; choose available ports starting at the saved settings.json ports.
call "%~dp0run.bat" --local %*
exit /b %errorlevel%
