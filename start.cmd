@echo off
rem Runs start.ps1 for this command only. Windows' execution policy setting is not changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %ERRORLEVEL%
