"""Omnes by Pham (package: datacleaner): convert spectrometer exports (UV-Vis, fluorescence, EEMs) to Excel and Origin projects.

    from datacleaner import run
    run("path/to/folder", merge=True, opju=True)
"""
__version__ = "13.0"
APP_NAME = "Omnes"
APP_BYLINE = "by Pham"

from .pipeline import run, scan_folder, scan_paths  # noqa: E402,F401
from .detect import FORMATS, detect  # noqa: E402,F401
