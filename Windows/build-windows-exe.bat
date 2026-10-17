@echo off
setlocal
REM Builds the standalone app: dist\Omnes\Omnes.exe (no Python needed to run it).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup-windows.bat || exit /b 1
call .venv\Scripts\activate.bat || exit /b 1
python -m pip install pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --windowed --name "Omnes" ^
  --icon assets\datacleaner.ico --add-data "assets;assets" ^
  --collect-data customtkinter --collect-data pptx --exclude-module scipy ^
  run_datacleaner.py || exit /b 1
echo.
echo Standalone build created at "dist\Omnes\Omnes.exe"
echo Copy the whole "dist\Omnes" folder (zipped) to share it - no Python needed on other PCs.
pause
