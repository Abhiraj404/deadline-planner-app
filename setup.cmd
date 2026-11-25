@echo off
rem Runs setup.ps1 for this command only. Windows' execution policy setting is not changed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
exit /b %ERRORLEVEL%
