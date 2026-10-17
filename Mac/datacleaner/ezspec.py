"""HORIBA EzSpec: .ezspec_data XML, session folders (Signals, corrections) and the binary .ezspec_raw."""
from __future__ import annotations

from .core import *  # noqa: F401,F403

# ================================================================ HORIBA EzSpec (.ezspec_data XML)

def read_ezspec(path: Path) -> list[dict]:
    """Every <FelixTrace> in an EzSpec XML file: name, x, y, properties, time."""
    traces = []
    for t in ET.parse(path).getroot().iter("FelixTrace"):
        pts = t.find("datapoints")
        xs = [float(p.findtext("X")) for p in pts] if pts is not None else []
        ys = [float(p.findtext("Y")) for p in pts] if pts is not None else []
        props = {}
        pe = t.find("properties")
        if pe is not None:
            for p in pe:
                if p.get("Name"):
                    props[p.get("Name")] = p.text
        traces.append({"name": t.findtext("Name") or "", "x": np.array(xs), "y": np.array(ys),
                       "props": props, "time": t.findtext("TimeExecuted")})
    return traces


def _excitation(tr: dict) -> Optional[float]:
    if tr["props"].get("acq.AcqExcWavelength"):
        return float(tr["props"]["acq.AcqExcWavelength"])
    m = re.search(r"\s(\d+(?:\.\d+)?):", tr["name"])
    return float(m.group(1)) if m else None


def _braced(name: str):
    """'CCD {DSI=0;...;Ex.=650;Ph.=Sample}' -> ('CCD', {'Ex.': '650', 'Ph.': 'Sample', ...})"""
    m = re.match(r"^(\w+) \{(.*)\}$", name)
    if not m:
        return None, {}
    return m.group(1), dict(kv.split("=", 1) for kv in m.group(2).split(";") if "=" in kv)


_INFO_PREFIXES = ("ibl.", "ui.EEM.", "acq.Acq", "dbl.")


def detect_ezspec(p: Path, t: str) -> bool:
    return p.suffix.lower() == ".ezspec_data" or "<felixsessiondata" in t[:600].lower()


def parse_ezspec(path: Path, text: str) -> list:
    traces = [t for t in read_ezspec(path) if len(t["x"])]
    eem_tr = [t for t in traces
              if t["props"].get("ibl.MethodType") == "MethodType_EEM"
              and "acq.AcqAxisLabel.3D" in t["props"] and _excitation(t) is not None]
    if not eem_tr:
        # Single scans (emission, excitation, absorbance...): one spectrum per trace.
        # A file with one trace is named after the file, like the exported data usually is.
        out = []
        for t in traces:
            name = path.stem if len(traces) == 1 else t["name"]
            out.append(make_series(t["x"], t["y"], name))
        return out

    eem = EEM(path.stem, _eem_frame({_excitation(t): make_series(t["x"], t["y"], "")
                                     for t in eem_tr}), source="ezspec")
    p0 = eem_tr[0]["props"]
    eem.info = {k: v for k, v in sorted(p0.items())
                if k.startswith(_INFO_PREFIXES) and k != "acq.HardwareConfig" and v is not None}
    eem.info["Acquired"] = eem_tr[0]["time"]
    eem.info["Correction formula"] = ("(CCD - CCD_dark) / (Ref - Ref_dark) "
                                      "* MCorrect(em) / XCorrect(ex)")
    _attach_session(eem, path.parent)
    return [eem]


# ---------------------------------------------------------------- session companions

def _attach_session(eem: EEM, folder: Path) -> None:
    sig_path = folder / "Signals.ezspec_data"
    order = None   # [(excitation, phase)] in acquisition order, used to label raw frames
    if sig_path.exists():
        order = _attach_signals(eem, sig_path)
    raws = sorted(folder.glob("*.ezspec_raw"))
    if raws:
        _attach_raw(eem, raws[0], order)


def _attach_signals(eem: EEM, path: Path) -> list:
    raw, dark, ref, trn, order = {}, None, {}, {}, []
    for t in read_ezspec(path):
        n = t["name"]
        if n.startswith("MCorrect"):
            s = make_series(t["x"], t["y"], n)
            eem.extras["Em correction"] = s.to_frame()
            eem.em_indexed.add("Em correction")
            continue
        if n.startswith("XCorrect"):
            s = make_series(t["x"], t["y"], n)
            s.index = [nice(v) for v in s.index]
            s.index.name = "Ex (nm)"
            eem.extras["Ex correction"] = s.to_frame()
            continue
        kind, kv = _braced(n)
        if kind is None or "Ex." not in kv:
            continue
        ex, phase = float(kv["Ex."]), kv.get("Ph.", "")
        if kind == "CCD":
            s = make_series(t["x"], t["y"], "")
            order.append((ex, phase))
            if phase == "Dark":
                dark = s
            else:
                raw[ex] = s
        elif kind == "Ref" and len(t["y"]):
            ref[(phase, ex)] = t["y"][0]
        elif kind == "Abs" and len(t["y"]):
            trn[(phase, ex)] = t["y"][0]

    if raw:
        counts = _eem_frame(raw)
        if dark is not None:
            counts["Dark"] = dark
        eem.extras["Raw counts"] = counts
        eem.em_indexed.add("Raw counts")

    d_ref = next((v for (ph, _), v in ref.items() if ph == "Dark"), np.nan)
    d_trn = next((v for (ph, _), v in trn.items() if ph == "Dark"), np.nan)
    exs = sorted(ex for (ph, ex) in ref if ph != "Dark")
    if exs:
        tbl = pd.DataFrame({
            "Ref (µA)": [ref[("Sample", ex)] if ("Sample", ex) in ref else np.nan for ex in exs],
            "Trans (µA)": [trn.get(("Sample", ex), np.nan) for ex in exs],
        }, index=pd.Index([nice(e) for e in exs], name="Ex (nm)"))
        tbl["Ref - dark"] = tbl["Ref (µA)"] - d_ref
        tbl["Trans - dark"] = tbl["Trans (µA)"] - d_trn
        tbl["Trans/Ref (dark-corrected)"] = tbl["Trans - dark"] / tbl["Ref - dark"]
        tbl.attrs["dark"] = (d_ref, d_trn)
        eem.extras["Ref & Trans"] = tbl
        eem.info["Ref dark (µA)"] = d_ref
        eem.info["Trans dark (µA)"] = d_trn
    return order


def _check_correction(eem: EEM) -> Optional[float]:
    """Recompute the corrected EEM from raw signals; return median |relative error| (%)."""
    need = ("Raw counts", "Ref & Trans", "Em correction", "Ex correction")
    if not all(k in eem.extras for k in need) or "Dark" not in eem.extras["Raw counts"]:
        return None
    raw = eem.extras["Raw counts"]
    m_corr = eem.extras["Em correction"].iloc[:, 0]
    x_corr = eem.extras["Ex correction"].iloc[:, 0]
    ref = eem.extras["Ref & Trans"]["Ref - dark"]
    cols = [c for c in eem.matrix.columns if c in raw.columns and c in ref.index and c in x_corr.index]
    calc = raw[cols].sub(raw["Dark"], axis=0).div(ref[cols], axis=1)
    calc = calc.mul(m_corr.reindex(calc.index), axis=0).div(x_corr[cols], axis=1)
    got = eem.matrix[cols].reindex(calc.index)
    mask = got.abs() > 10
    if not mask.values.any():
        return None
    rel = ((calc - got).abs() / got.abs())[mask]
    return float(np.nanmedian(rel.values)) * 100


# ---------------------------------------------------------------- binary .ezspec_raw

def read_ezspec_raw(path: Path) -> list:
    """Decode EzSpec's binary raw file into chunks.

    Layout: 'EZSPECRAWVER' + 4-char version, then a sequence of chunks, each starting
    with 'EZSPECDATA' followed by:
      'DARRAY' + 4-char tag + uint32 count + uint16 element size + count*size bytes
               (size 1 = text, 4 = float32 little-endian)
      'RECORD' + 4-char tag + uint16 size + payload (float32/uint32 value + uint16 frame #)
      'CNTNXT' (continuation marker, no payload) | 'LAST' (end of file)
    """
    b = path.read_bytes()
    if not b.startswith(b"EZSPECRAWVER"):
        raise ValueError("not an EzSpec raw file")
    i, out = 16, []
    while i < len(b):
        if b[i:i + 10] != b"EZSPECDATA":
            raise ValueError(f"unexpected bytes at offset {i}")
        i += 10
        kind = b[i:i + 6]
        if kind == b"DARRAY":
            tag = b[i + 6:i + 10].decode("ascii", "replace")
            n, size = struct.unpack_from("<IH", b, i + 10)
            i += 16
            data = b[i:i + n * size]
            i += n * size
            if size == 1:
                val = data.decode("utf-8", "replace")
            elif size == 4:
                val = np.frombuffer(data, "<f4").astype(float)
            elif size == 8:
                val = np.frombuffer(data, "<f8")
            else:
                val = data
            out.append(("A", tag, val))
        elif kind == b"RECORD":
            tag = b[i + 6:i + 10].decode("ascii", "replace")
            (size,) = struct.unpack_from("<H", b, i + 10)
            i += 12
            out.append(("R", tag, b[i:i + size]))
            i += size
        elif kind == b"CNTNXT":
            i += 6
        elif b[i:i + 4] == b"LAST":
            break
        else:
            raise ValueError(f"unknown chunk at offset {i}")
    return out


_RAW_META = {"EXF": "Ex filter", "EMF": "Em filter", "EXB": "Ex bandwidth (nm)",
             "EMB": "Em bandwidth (nm)", "EXP": "Ex polarizer", "EMP": "Em polarizer",
             "EXS": "Ex shutter", "EMS": "Em shutter"}


def _raw_frames(chunks: list) -> list[dict]:
    """Group chunks into frames: [settings...] CCDY TIME RREF RTRN.
    Note: the settings chunks written before frame k+1 describe frame k (verified against
    the XML signals), so they are shifted back by one here. The last frame is the dark."""
    blocks, cur, ccdx = [], {}, None
    for c in chunks:
        kind, tag, val = c
        if kind == "A":
            if tag == "CCDX":
                ccdx = val
            elif tag == "CCDY":
                cur["counts"], cur["pixels_nm"] = val, ccdx
            elif tag.endswith("V") and isinstance(val, str) and tag[:3] in _RAW_META:
                cur[tag[:3]] = val
        else:
            if tag == "EXWL":
                cur["EXWL"] = struct.unpack_from("<f", val)[0]
            elif tag == "RREF":
                cur["ref"] = struct.unpack_from("<f", val)[0]
            elif tag == "RTRN":
                cur["trans"] = struct.unpack_from("<f", val)[0]
                blocks.append(cur)
                cur = {}
            elif tag == "TIME":
                cur["time_s"] = struct.unpack_from("<I", val)[0] / 1000
    frames = []
    for k, blk in enumerate(blocks):
        nxt = blocks[k + 1] if k + 1 < len(blocks) else {}
        f = {"counts": blk["counts"], "pixels_nm": blk["pixels_nm"], "time_s": blk.get("time_s"),
             "ref": blk.get("ref"), "trans": blk.get("trans"), "ex": nxt.get("EXWL")}
        for key, label in _RAW_META.items():
            v = nxt.get(key)
            f[label] = None if v in (None, "meta_data_template") else v
        frames.append(f)
    if frames:
        frames[-1]["ex"] = None   # dark frame
    return frames


def _attach_raw(eem: EEM, path: Path, order: Optional[list]) -> None:
    frames = _raw_frames(read_ezspec_raw(path))
    if not frames:
        return
    if order and len(order) == len(frames):
        labels = [("Dark" if ph == "Dark" else nice(ex)) for ex, ph in order]
    else:
        labels = [("Dark" if f["ex"] is None else nice(f["ex"])) for f in frames]
    cols = {lab: make_series(f["pixels_nm"], f["counts"], "") for lab, f in zip(labels, frames)}
    full = pd.concat(cols, axis=1).sort_index()
    full.index.name = WL
    eem.extras["Full CCD (raw)"] = full
    eem.em_indexed.add("Full CCD (raw)")
    meta = pd.DataFrame([{"Frame": i + 1, "Excitation (nm)": lab, "Time (s)": f["time_s"],
                          "Ref (µA)": f["ref"], "Trans (µA)": f["trans"],
                          **{v: f[v] for v in _RAW_META.values()}}
                         for i, (lab, f) in enumerate(zip(labels, frames))]).set_index("Frame")
    eem.extras["Frames"] = meta



__all__ = [n for n in dir() if not n.startswith("__")]
