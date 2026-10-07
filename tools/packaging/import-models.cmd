@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0import-models.ps1"
set "ZHILAN_IMPORT_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %ZHILAN_IMPORT_EXIT%
