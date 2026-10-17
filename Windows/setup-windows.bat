@echo off
setlocal
REM One-time setup: makes a private Python environment (.venv) with Omnes's packages.
cd /d "%~dp0"
where py >nul 2>nul || (
  echo Python is required. Install Python 3.10 or newer from https://www.python.org/downloads/windows/
  echo During installation, select "Add Python to PATH".
  pause
  exit /b 1
)
py -3 -m venv .venv || exit /b 1
call .venv\Scripts\activate.bat || exit /b 1
python -m pip install --upgrade pip || exit /b 1
python -m pip install -r requirements.txt || exit /b 1
echo.
echo Omnes is ready. Run run-omnes.bat to start it.
pause
