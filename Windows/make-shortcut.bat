@echo off
setlocal
REM Puts a "Omnes" shortcut, with its icon, on the Desktop.
cd /d "%~dp0"
if not exist "dist\Omnes\Omnes.exe" if not exist ".venv\Scripts\pythonw.exe" call setup-windows.bat || exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make-shortcut.ps1"
pause
