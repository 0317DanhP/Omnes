"""Parsers for 1-D spectra and EEM exports (Cary, JCAMP, Ocean Optics, Duetta, K2, FBSW, EEM .dat)."""
from __future__ import annotations

from .core import *  # noqa: F401,F403

# ================================================================ 1-D parsers
# Each parser takes (path, text) and returns a list of pd.Series and/or EEM objects.

def parse_cary(path: Path, text: str) -> list:
    """Row 1: sample names (every other column). Row 2: Wavelength/Abs headers.
    Each sample keeps its own wavelength column, so differing grids are preserved."""
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        return []
    names, header = rows[0], rows[1]
    data = []
    for r in rows[2:]:
        if not any(c.strip() for c in r):   # blank row = start of the instrument's metadata
            break
        data.append(r)

    def get(r, i):
        return r[i] if i < len(r) else ""

    out = []
    for i in range(0, len(header) - 1, 2):
        name = get(names, i).strip() or f"{path.stem} #{i // 2 + 1}"
        s = make_series((get(r, i) for r in data), (get(r, i + 1) for r in data), name)
        if len(s) >= MIN_POINTS:
            out.append(s)
    eem = _eem_from_named(out, path, "csv")
    return [eem] if eem else out


_EX_NAME = re.compile(r"^(.*?)_EX_(\d+(?:\.\d+)?)$", re.I)


def _eem_from_named(spectra: list, path: Path, source: str) -> Optional["EEM"]:
    """Cary Eclipse names each scan of an EEM '<sample>_EX_<nm>'. If every scan is named that
    way for one sample with distinct excitations, return them as an EEM."""
    if len(spectra) < 3:
        return None
    ms = [_EX_NAME.match(s.name or "") for s in spectra]
    if not all(ms):
        return None
    prefixes = {m.group(1) for m in ms}
    exs = [float(m.group(2)) for m in ms]
    if len(prefixes) != 1 or len(set(exs)) != len(exs):
        return None
    return EEM(prefixes.pop() or path.stem,
               _eem_frame({ex: s.rename(None) for ex, s in zip(exs, spectra)}), source=source)


def parse_jcamp(path: Path, text: str) -> list:
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(r"\s*##\s*XYDATA", l, re.I))
    if "X++" in lines[start].replace(" ", "").upper():
        raise ValueError("compressed X++(Y..Y) JCAMP data isn't supported")
    wl, y = [], []
    for l in lines[start + 1:]:
        if l.lstrip().startswith("##"):
            break
        f = fields(l)
        for j in range(0, len(f) - 1, 2):      # handles one or several X,Y pairs per line
            if is_num(f[j]) and is_num(f[j + 1]):
                wl.append(f[j])
                y.append(f[j + 1])
    return [make_series(wl, y, path.stem)]


def parse_ocean(path: Path, text: str) -> list:
    lines = text.splitlines()
    start = next((i + 1 for i, l in enumerate(lines)
                  if re.search(r"begin.*spectral data", l, re.I)), 0)
    return [two_col(numeric_block(lines, start), path.stem.split("_USB4F")[0])]


def parse_duetta_txt(path: Path, text: str) -> list:
    lines = text.splitlines()
    name = path.name.split(". TableHeaderData")[0]
    if name == path.name:
        name = path.stem
    try:
        name = f"{name}   ({lines[7].split(chr(9))[1].split(' ')[1]})"   # acquisition date
    except IndexError:
        pass
    wl, y = [], []
    for l in lines[12:]:
        f = [c.strip() for c in l.split("\t")]
        if len(f) >= 3 and is_num(f[1]) and is_num(f[2]):
            wl.append(f[1])
            y.append(f[2])
    return [make_series(wl, y, name)]


def parse_k2(path: Path, text: str) -> list:
    return [two_col(numeric_block(text.splitlines()), path.stem)]   # stderr column dropped


def parse_dat(path: Path, text: str) -> list:
    lines = text.splitlines()
    name = path.stem
    if len(lines) > 1 and ":" in lines[1].split(",")[0]:
        name = lines[1].split(",")[0].split(":", 1)[1].strip() or name
    return [two_col(numeric_block(lines), name)]


def parse_generic(path: Path, text: str) -> list:
    return [two_col(numeric_block(text.splitlines()), path.stem)]


# ================================================================ Varian Cary Eclipse .FBSW

FBSW_MAGIC = b"\x11Varian"
_FBSW_CHUNK = b"TContinuumStore"


def is_fbsw(p: Path) -> bool:
    if p.suffix.lower() == ".fbsw":
        return True
    with open(p, "rb") as f:
        return f.read(8) == FBSW_MAGIC


def read_fbsw(path: Path) -> list[dict]:
    """Decode a Cary Eclipse batch file (.FBSW).

    Layout (reverse-engineered): header '\\x11Varian UV-VIS-NIR', then one 'TContinuumStore'
    chunk per scan:
        uint32 chunk size, uint32, uint32, float32 x_min, float32 x_max, float32 y_min,
        float32 y_max, uint32 n_points, ... settings ...,
        n_points x (float32 x, float32 y),
        uint32-length-prefixed text: scan name, then the method report
        ('Scan mode   Emission', 'Ex. Wavelength (nm)   250.00', ...)
    Report/graph stores (TReportStore, TGraphStore, TDatabaseStore) follow the scans."""
    b = path.read_bytes()
    offs = [m.start() for m in re.finditer(_FBSW_CHUNK, b)]
    traces = []
    for k, o in enumerate(offs):
        h = o + len(_FBSW_CHUNK)
        if h + 32 > len(b):
            continue
        _, _, _, x_min, x_max, _, _, n = struct.unpack_from("<IIIffffI", b, h)
        limit = offs[k + 1] if k + 1 < len(offs) else len(b)
        if not 0 < n < 10_000_000:
            continue
        key = struct.pack("<f", x_min)
        p, start = b.find(key, h + 32, limit), None
        while p != -1 and p + 8 * n <= limit:          # data: first x = x_min, last x = x_max
            if struct.unpack_from("<f", b, p + 8 * (n - 1))[0] == x_max:
                start = p
                break
            p = b.find(key, p + 1, limit)
        if start is None:
            continue
        xy = np.frombuffer(b, "<f4", count=2 * n, offset=start).reshape(n, 2).astype(float)
        q, texts, zeros = start + 8 * n, [], 0
        while q + 4 <= limit and zeros < 3:
            (ln,) = struct.unpack_from("<I", b, q)
            if ln == 0:
                zeros += 1
                q += 4
                continue
            raw = b[q + 4:q + 4 + ln]
            if ln > 4000 or len(raw) < ln or any(c < 32 and c != 9 for c in raw):
                break
            texts.append(raw.decode("latin-1"))
            zeros = 0
            q += 4 + ln
        meta = "\n".join(texts[1:])
        ex = re.search(r"Ex\. Wavelength \(nm\)\s+([\d.]+)", meta)
        mode = re.search(r"Scan mode\s+(\w+)", meta)
        traces.append({"name": texts[0] if texts else f"{path.stem} #{k + 1}",
                       "x": xy[:, 0], "y": xy[:, 1], "meta": meta,
                       "ex": float(ex.group(1)) if ex else None,
                       "mode": mode.group(1) if mode else None})
    return traces


def _report_settings(meta: str) -> dict:
    """'Instrument                        Cary Eclipse' -> {'Instrument': 'Cary Eclipse'}"""
    out = {}
    for line in meta.splitlines():
        parts = re.split(r"\s{2,}|\s+:\s*", line.strip(), maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1]:
            out.setdefault(parts[0].strip(), parts[1].strip())
    return out


def parse_fbsw(path: Path, text: str) -> list:
    traces = read_fbsw(path)
    if not traces:
        return []
    # In an EEM batch every scan's report repeats the method's *starting* excitation, so the
    # scan name ('S503_EX_260.00') is the reliable source; the report is the fallback.
    exs = []
    for t in traces:
        m = _EX_NAME.match(t["name"])
        exs.append(float(m.group(2)) if m else t["ex"])
    is_eem = (len(traces) >= 3 and None not in exs and len(set(exs)) == len(exs)
              and all((t["mode"] or "").lower().startswith("emission") for t in traces))
    if not is_eem:   # e.g. several samples measured at one excitation
        return [make_series(t["x"], t["y"], t["name"]) for t in traces]
    m = _EX_NAME.match(traces[0]["name"])
    prefixes = {(_EX_NAME.match(t["name"]) or m).group(1) for t in traces} if m else set()
    name = prefixes.pop() if len(prefixes) == 1 else path.stem
    eem = EEM(name, _eem_frame({ex: make_series(t["x"], t["y"], "") for ex, t in zip(exs, traces)}),
              source="fbsw")
    info = _report_settings(traces[0]["meta"])
    info.pop("Ex. Wavelength (nm)", None)
    info["Ex. Wavelengths (nm)"] = f"{nice(min(exs))}-{nice(max(exs))} ({len(exs)} scans)"
    eem.info = info
    return [eem]


# ================================================================ Duetta EEM .dat

def _eem_dat_header(text: str) -> list[str]:
    first = text.split("\n", 1)[0].rstrip("\r").split("\t")
    return first


def detect_eem_dat(p: Path, t: str) -> bool:
    h = _eem_dat_header(t)
    exs = [x for x in h[1:] if x.strip()]
    return len(exs) >= 3 and not is_num(h[0]) and all(is_num(x) for x in exs)


def parse_eem_dat(path: Path, text: str) -> list:
    """Line 1: 'Sample <ex1> <ex2> ...'; then 'nm', 'Normalized by ...'; then
    one row per emission wavelength: em, I(ex1), I(ex2), ..."""
    lines = text.splitlines()
    exs = [float(x) for x in _eem_dat_header(text)[1:] if x.strip()]
    n = len(exs)
    em, rows, info = [], [], {}
    for l in lines[1:]:
        f = l.split("\t")
        if len(f) >= n + 1 and is_num(f[0]):
            em.append(float(f[0]))
            rows.append([float(v) if is_num(v) else np.nan for v in f[1:n + 1]])
        elif l.strip() and not rows:
            info[f"Header line {len(info) + 2}"] = l.strip()
    arr = np.array(rows, dtype=float)
    cols = {ex: pd.Series(arr[:, j], index=np.round(em, WL_DECIMALS)) for j, ex in enumerate(exs)}
    return [EEM(path.stem, _eem_frame(cols), info=info, source="dat")]



__all__ = [n for n in dir() if not n.startswith("__")]
