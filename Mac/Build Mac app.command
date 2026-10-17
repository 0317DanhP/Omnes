#!/bin/bash
# Double-click in Finder: builds "Omnes.app" into dist/ and zips it.
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || bash setup-mac.sh
.venv/bin/python -m pip install pyinstaller
.venv/bin/python -m PyInstaller --noconfirm --clean --windowed --name "Omnes" \
  --icon assets/datacleaner.icns --osx-bundle-identifier com.pham.omnes \
  --add-data "assets:assets" --collect-data customtkinter --collect-data pptx --exclude-module scipy \
  run_datacleaner.py
cd dist
rm -f "Omnes-mac.zip"
ditto -c -k --sequesterRsrc --keepParent "Omnes.app" "Omnes-mac.zip"
echo
echo "Built dist/Omnes.app (+ Omnes-mac.zip to share)."
echo "It runs on Macs with the same chip as this one ($(uname -m))."
