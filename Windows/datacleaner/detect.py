"""Format registry and auto-detection."""
from __future__ import annotations

from .core import *  # noqa: F401,F403
from .parsers import *  # noqa: F401,F403
from .ezspec import *  # noqa: F401,F403

# ================================================================ detection

def _head(text: str, n: int = 40) -> str:
    return "\n".join(text.splitlines()[:n]).lower()


def _cary_like(p: Path, t: str) -> bool:
    lines = t.splitlines()
    if p.suffix.lower() != ".csv" or len(lines) < 2:
        return False
    return lines[1].split(",")[0].strip().strip('"').lower().startswith("wavelength")


@dataclass
class Format:
    label: str
    parse: Callable[[Path, str], list]
    detect: Callable[[Path, str], bool]


# Checked in this order; the first match wins.
FORMATS: dict[str, Format] = {
    "fbsw": Format("Varian Cary Eclipse (.FBSW)", parse_fbsw, lambda p, t: is_fbsw(p)),
    "ezspec": Format("HORIBA EzSpec (.ezspec_data)", parse_ezspec, detect_ezspec),
    "jcamp": Format("Agilent Diode Array (.dx)", parse_jcamp,
                    lambda p, t: bool(re.search(r"^\s*##\s*XYDATA", t, re.I | re.M))),
    "ocean": Format("Ocean Optics (.txt)", parse_ocean,
                    lambda p, t: "_usb4f" in p.name.lower()
                    or bool(re.search(r"begin.*spectral data|spectrasuite|oceanview", _head(t)))),
    "duetta_txt": Format("Duetta (.txt)", parse_duetta_txt,
                         lambda p, t: "tableheaderdata" in p.name.lower()
                         or "tableheaderdata" in _head(t)),
    "cary": Format("Varian / Agilent Cary (.csv)", parse_cary, _cary_like),
    "eem_dat": Format("Duetta EEM (.dat)", parse_eem_dat, detect_eem_dat),
    "k2": Format("K2 ISS (.ifx)", parse_k2, lambda p, t: p.suffix.lower() == ".ifx"),
    "dat": Format("FT-300 / Duetta (.dat)", parse_dat, lambda p, t: p.suffix.lower() == ".dat"),
    "generic": Format("Generic two-column", parse_generic,
                      lambda p, t: len(numeric_block(t.splitlines())) >= MIN_POINTS),
}


def detect(path: Path, text: str) -> Optional[str]:
    return next((k for k, f in FORMATS.items() if f.detect(path, text)), None)



__all__ = [n for n in dir() if not n.startswith("__")]
