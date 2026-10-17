#!/bin/bash
# One-time setup on a Mac: makes a private Python environment (.venv) with Omnes's packages.
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "Python 3 is needed - install it from https://www.python.org/downloads/macos/"; exit 1; }
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo
echo "Omnes is ready. Double-click 'Run Omnes.command'."
