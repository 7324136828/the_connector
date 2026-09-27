@echo off
rem Local launch: UI 5130, API 8301, Kokoro 8302.
call "%~dp0run_lan.bat" -Local %*
exit /b %errorlevel%
