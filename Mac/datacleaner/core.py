"""Shared constants, parsing helpers and the EEM data model."""
from __future__ import annotations

import csv
import io
import re
import struct
import sys
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

WL = "Wavelength (nm)"
EM_EX = WL   # first column of an EEM sheet (rows are emission wavelengths)
WL_DECIMALS = 4   # wavelengths are rounded to this many decimals before aligning
MIN_POINTS = 5    # fewer numeric rows than this counts as "no data found"
OUTPUT_DIR = "cleaned"
SKIP_SUFFIXES = {".xlsx", ".xls", ".xlsm", ".opju", ".opj", ".py", ".exe", ".ico",
                 ".png", ".jpg", ".jpeg", ".pdf", ".docx", ".pptx", ".zip", ".ipynb"}
# Files inside an EzSpec session folder that are read as part of the session, not on their own
EZSPEC_COMPANIONS = {"signals.ezspec_data", "corrections.ezspec_data", "ezspec.log", "icl_log.txt"}
EZSPEC_COMPANION_SUFFIXES = {".ezspec_raw", ".ezspec_method"}
# When the same EEM shows up in several forms, keep the most complete one
EEM_SOURCE_RANK = {"ezspec": 0, "fbsw": 1, "csv": 2, "dat": 3}

_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_SPLIT = re.compile(r"[\t,;]+|\s+")


# ================================================================ helpers

def is_num(s: str) -> bool:
    return bool(_NUM.match(s.strip().strip('"')))


def fields(line: str) -> list[str]:
    return [f.strip('"') for f in _SPLIT.split(line.strip()) if f]


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def looks_binary(path: Path) -> bool:
    with open(path, "rb") as f:
        return b"\x00" in f.read(2048)


def nice(v):
    """650.0 -> 650 for column headers; leaves non-integers alone."""
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


def make_series(wl, y, name: str) -> pd.Series:
    """One spectrum: values indexed by (rounded) wavelength, blanks and duplicates removed."""
    wl = pd.to_numeric(pd.Series(list(wl), dtype="object"), errors="coerce").round(WL_DECIMALS)
    y = pd.to_numeric(pd.Series(list(y), dtype="object"), errors="coerce")
    s = pd.Series(y.to_numpy(dtype=float), index=wl.to_numpy(dtype=float), name=str(name).strip())
    s = s[(~pd.isna(s.index)) & s.notna().to_numpy()]
    s = s[~s.index.duplicated(keep="first")]
    s.index.name = WL
    return s.sort_index()


def numeric_block(lines: list[str], start: int = 0) -> list[list[str]]:
    """Longest run of consecutive lines whose first two fields are numbers."""
    best, run = (0, 0), None
    for i in range(start, len(lines) + 1):
        ok = False
        if i < len(lines):
            f = fields(lines[i])
            ok = len(f) >= 2 and is_num(f[0]) and is_num(f[1])
        if ok and run is None:
            run = i
        elif not ok and run is not None:
            if i - run > best[1] - best[0]:
                best = (run, i)
            run = None
    return [fields(l) for l in lines[best[0]:best[1]]]


def two_col(rows: list[list[str]], name: str) -> pd.Series:
    return make_series((r[0] for r in rows), (r[1] for r in rows), name)


def integer_bins(index) -> np.ndarray:
    """Whole-nm bin for each wavelength; halves round up (499.5-500.49 -> 500)."""
    return np.floor(np.asarray(index, dtype=float) + 0.5).astype(int)


def average_to_integers(obj):
    """Average every row whose wavelength rounds to the same whole nm (Series or DataFrame)."""
    out = obj.groupby(integer_bins(obj.index)).mean()
    out.index.name = obj.index.name
    if isinstance(obj, pd.Series):
        out.name = obj.name
    return out


# ================================================================ EEM container

@dataclass
class EEM:
    name: str
    matrix: pd.DataFrame                              # rows: emission nm, columns: excitation nm
    extras: dict = field(default_factory=dict)        # sheet name -> DataFrame
    em_indexed: set = field(default_factory=set)      # extras whose rows are emission wavelengths
    info: dict = field(default_factory=dict)
    source: str = ""                                  # "ezspec", "fbsw", "csv" or "dat"


def _eem_frame(columns: dict) -> pd.DataFrame:
    """{excitation: emission Series} -> matrix: ascending emission rows, excitation columns
    named by wavelength only (250, 260, ...) and kept in the order the instrument scanned them."""
    df = pd.concat({nice(k): v for k, v in columns.items()}, axis=1).sort_index()
    df.index.name = EM_EX
    return df



_BAD_SHEET = re.compile(r"[\[\]:*?/\\]")


def _sheet_name(name: str, used: set) -> str:
    base = _BAD_SHEET.sub("_", str(name))[:31] or "Sheet"
    s, k = base, 2
    while s.lower() in used:
        suffix = f" ({k})"
        s = base[:31 - len(suffix)] + suffix
        k += 1
    used.add(s.lower())
    return s


__all__ = [n for n in dir() if not n.startswith("__")]
