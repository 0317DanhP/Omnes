# Omnes for Windows

## Use it
1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/windows/), and tick **Add Python to PATH**.
2. Double-click `setup-windows.bat` once.
3. Double-click `run-omnes.bat`.
   `make-shortcut.bat` puts a Omnes shortcut, with its icon, on your Desktop.

## Make the app
`build-windows-exe.bat` builds `dist\Omnes\Omnes.exe`.
Share the whole `dist\Omnes` folder, zipped. Nobody needs Python to run it.
(Or download the ready-made `Omnes-windows.zip` from the repository's **Releases** page.)

## Command line
```
.venv\Scripts\python -m datacleaner "D:\data\run 12" --opju --pptx
```
`python -m datacleaner --help` lists every option.
