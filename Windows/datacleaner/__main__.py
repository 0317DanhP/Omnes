"""`python -m datacleaner` opens the GUI; `python -m datacleaner FOLDER [options]` runs headless."""
import argparse
import sys


def cli(argv=None) -> int:
    p = argparse.ArgumentParser(prog="omnes", description=__doc__)
    p.add_argument("inputs", nargs="*", metavar="FOLDER_OR_FILES",
                   help="a folder, or one or more data files (omit to open the GUI)")
    p.add_argument("--separate", action="store_true", help="one workbook per input file instead of one per folder")
    p.add_argument("--opju", action="store_true", help="also write an Origin project")
    p.add_argument("--contour", action="store_true", help="EEM contour graphs in the Origin project (experimental)")
    p.add_argument("--pptx", action="store_true", help="export the graphs to a PowerPoint deck")
    p.add_argument("--average", action="store_true", help="average to integer wavelengths")
    p.add_argument("--interpolate", action="store_true", help="interpolate across different wavelength grids")
    p.add_argument("--recursive", action="store_true", help="include subfolders")
    a = p.parse_args(argv)
    if not a.inputs:
        from .gui import main
        main()
        return 0
    from .pipeline import run
    from pathlib import Path
    if len(a.inputs) == 1 and Path(a.inputs[0]).is_dir():
        folder, files = a.inputs[0], None
    else:
        folder, files = None, a.inputs
    written = run(folder, files=files, merge=not a.separate, interpolate=a.interpolate, average=a.average,
                  recursive=a.recursive, opju=a.opju, contour=a.contour, pptx=a.pptx)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(cli())
