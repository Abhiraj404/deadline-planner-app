@echo off
rem Runs stop.ps1 for this command only. Windows' execution policy setting is not changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
exit /b %ERRORLEVEL%
