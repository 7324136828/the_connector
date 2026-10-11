@echo off
rem run.py binds all services to LAN interfaces unless --local or --host is given.
call "%~dp0run.bat" %*
exit /b %errorlevel%
