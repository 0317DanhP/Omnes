"""Double-click / PyInstaller entry point: opens the Omnes window."""
from datacleaner.__main__ import cli

if __name__ == "__main__":
    raise SystemExit(cli())
