@echo off
setlocal
REM Starts Omnes (sets it up the first time).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Omnes is not installed yet. Running setup...
  call setup-windows.bat || exit /b 1
)
start "Omnes" ".venv\Scripts\pythonw.exe" "%~dp0run_datacleaner.py" %*
