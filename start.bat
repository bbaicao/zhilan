@echo off
cd /d "%~dp0"
if not exist "%~dp0backend\.venv\Scripts\python.exe" (
  echo Missing Python venv.
  echo Run from the repository root:
  echo py -3.13 -m venv backend\.venv
  echo backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
  echo Then follow the source setup steps in README.md for configuration and frontend build.
  pause
  exit /b 1
)
"%~dp0backend\.venv\Scripts\python.exe" -m client
if errorlevel 1 pause