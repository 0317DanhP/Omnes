# Omnes for Mac

## Use it
1. Install Python 3 from [python.org](https://www.python.org/downloads/macos/), if it isn't installed already.
2. Double-click **Run Omnes.command**. The first run sets everything up; it takes a minute or two.

If macOS says a `.command` file can't be opened, open Terminal in this folder and run `chmod +x *.command *.sh`
once. Files downloaded from GitHub lose their "run me" permission.

## Make the app
Double-click **Build Mac app.command**. You get `dist/Omnes.app`, plus a zip to share. Builds run on
Macs with the same chip type (Apple Silicon or Intel) as the Mac that built them. The app isn't signed by
Apple, so on first launch right-click → **Open** → **Open**.
(Or download `Omnes-mac-apple-silicon.zip` / `Omnes-mac-intel.zip` from **Releases**.)

## Command line
```
.venv/bin/python -m datacleaner ~/data/run12 --opju --pptx
```
