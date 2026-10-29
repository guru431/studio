@echo off
rem Studio Agent: install from the studio archive (double click). Details: install.ps1, README.md.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set code=%errorlevel%
if not "%code%"=="0" pause
exit /b %code%
