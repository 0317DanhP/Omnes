"""
opju_build - write new Origin .opju projects (workbooks, sheets, a line graph) without Origin.

How it works
------------
An .opju is a header followed by a flat stream of blocks (see Principio's opju_format):
    datasets      one per column: rle(147-byte header) + data blob + raw(SYSTEM storage)
    windows       workbook: rle(195 window header) + script storage, then per sheet
                  rle(405 layer header) + raw(sheet name) + formatting (incl. an LZ4 grid block
                  holding the sheet name) + one group per column: rle(519 column record) +
                  raw(long name), then a fixed trailer; then a preview picture.
                  graph: window/layer/axis/text records, then one group per plot:
                  rle(519 plot record) + raw(1118 style), then axis records and a preview.
    notes         ResultsLog
    project tree  root folder (project name) listing every window by index
    directory     (offset, size) of each window preview; then project-level storage.
Fields this builder sets (all reverse-engineered from projects saved by OriginPro 2023, CPYUA 4.3683):
    dataset header  0x19 allocated rows, 0x21 rows, 0x52 X dataset of a Y column, 0x58 name
    column record   0x04 dataset #, 0x0B rows, 0x11 role (3 = X, 0 = Y), 0x12 short name,
                    0x23 X dataset #, followed by the long name
    sheet header    0x2B / 0x151 column count, 0x52 rows + 1, 0x12D sheet index
    window header   0x02 name, 0x29 active sheet
    plot record     0x04 Y dataset #, 0x23 X dataset #, 0x0B rows, 0x16A colour
    graph layer     0x0F.. X from/to/step, 0x3A.. Y from/to/step
    project tree    count byte = 2 x windows, then one 8-byte entry (0, index) per window
Everything else is copied from a project OriginPro wrote.  Experimental: keep checking that
the files open in OriginPro.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_PIECES = None


def _pieces() -> dict:
    global _PIECES
    if _PIECES is None:
        raw = json.loads(zlib.decompress(base64.b64decode(_TEMPLATE)))
        _PIECES = {k: base64.b64decode(v) for k, v in raw.items()}
    return _PIECES


# ============================================================== low-level encoders
def leb(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def rle_encode(raw: bytes) -> bytes:
    out, lit = bytearray(), bytearray()
    i, n = 0, len(raw)

    def flush():
        nonlocal lit
        while lit:
            ch = lit[:127]
            out.append(len(ch))
            out.extend(ch)
            lit = lit[127:]

    while i < n:
        b = raw[i]
        j = i
        while j < n and raw[j] == b and j - i < 66:
            j += 1
        if j - i >= 3:
            flush()
            if b == 0:
                out.append(0x80 + j - i - 3)
            else:
                out += bytes([0xC0 + j - i - 3, b])
            i = j
        else:
            lit.append(b)
            i += 1
    flush()
    return bytes(out)


def rle_decode(buf: bytes) -> bytes:
    out, p = bytearray(), 0
    while p < len(buf):
        c = buf[p]
        p += 1
        if c < 0x80:
            out += buf[p:p + c]
            p += c
        elif c < 0xC0:
            out += bytes(c - 0x80 + 3)
        else:
            out += bytes([buf[p]]) * (c - 0xC0 + 3)
            p += 1
    return bytes(out)


def rle_block(raw: bytes) -> bytes:
    enc = rle_encode(raw)
    return b"\x80" + leb(len(enc)) + enc


def raw_block(payload: bytes) -> bytes:
    hdr = leb(len(payload))
    if hdr[0] in (0x80, 0x81):          # ambiguous with RLE/LZ4 markers; Origin never writes these
        raise ValueError("raw block size not storable")
    return hdr + payload + b"\n"


def safe_text_block(text: str, terminator: bytes = b"\0") -> bytes:
    payload = text.encode("utf-8") + terminator
    if leb(len(payload))[0] in (0x80, 0x81):
        payload = text.encode("utf-8") + b" " + terminator
    return raw_block(payload)


def lz4_stored_block(raw: bytes) -> bytes:
    """LZ4 block kept uncompressed (decoded size == stored size), as Origin writes small ones."""
    return b"\x81" + leb(len(raw)) + leb(len(raw)) + raw


# ---- column data: FPC-compressed doubles in Origin's segment format
_PAY = (0, 1, 2, 3, 5, 6, 7, 8)
_M64 = (1 << 64) - 1
SEG_FPC, SEG_MISSING = 0x0C, 0x64


def fpc_encode(values: Sequence[float], tbits: int = 12) -> bytes:
    mask = (1 << tbits) - 1
    fcm = [0] * (1 << tbits)
    dfcm = [0] * (1 << tbits)
    fh = dh = last = 0
    nibs, pays = [], []
    for x in values:
        v = struct.unpack("<Q", struct.pack("<d", x))[0]
        rf = v ^ fcm[fh]
        rd = v ^ ((dfcm[dh] + last) & _M64)
        use_d = rd < rf
        r = rd if use_d else rf
        nb = (r.bit_length() + 7) // 8
        c = next(i for i, p in enumerate(_PAY) if p >= nb)
        nibs.append((8 if use_d else 0) | c)
        pays.append(r.to_bytes(_PAY[c], "little"))
        fcm[fh] = v
        fh = ((fh << 6) ^ (v >> 48)) & mask
        d = (v - last) & _M64
        dfcm[dh] = d
        dh = ((dh << 2) ^ (d >> 40)) & mask
        last = v
    out = bytearray()
    prev_ctrl = None
    for i in range(0, len(nibs), 2):
        hi = nibs[i + 1] if i + 1 < len(nibs) else 0
        ctrl = nibs[i] | (hi << 4)
        if ctrl == 0 and prev_ctrl == 0:
            # Origin never writes two all-zero control bytes in a row and reads them back as
            # missing values; spell the zero residual with one explicit 0x00 byte instead.
            nibs[i], pays[i] = 1, b"\x00"
            ctrl = 1
        out.append(ctrl)
        out += pays[i]
        if i + 1 < len(nibs):
            out += pays[i + 1]
        prev_ctrl = ctrl
    return bytes(out)


def _seg_header(cnt: int, codec: int) -> bytes:
    return leb(2 * cnt - 1 if codec == SEG_FPC else 2 * cnt) + bytes([codec])


CONST_MIN_RUN = 4      # Origin stores 4+ identical values as a constant segment, never inside FPC


def _const_segment(cnt: int, v: float) -> bytes:
    """Run of one value: codec 8 + 9k, followed by the top k bytes of the double."""
    b = struct.pack("<d", v)
    k = 8 - next((i for i in range(8) if b[i]), 8)         # drop the all-zero low bytes
    return _seg_header(cnt, 8 + 9 * k) + b[8 - k:]


def encode_segments(values: List[float]) -> bytes:
    out = bytearray()
    i, n = 0, len(values)
    fpc_start = None

    def flush_fpc(end):
        nonlocal fpc_start
        if fpc_start is not None and end > fpc_start:
            out.extend(_seg_header(end - fpc_start, SEG_FPC) + fpc_encode(values[fpc_start:end]))
        fpc_start = None

    while i < n:
        v = values[i]
        j = i + 1
        if v != v:
            while j < n and values[j] != values[j]:
                j += 1
            flush_fpc(i)
            out += _seg_header(j - i, SEG_MISSING)
        else:
            while j < n and values[j] == v and struct.pack("<d", values[j]) == struct.pack("<d", v):
                j += 1
            if j - i >= CONST_MIN_RUN:
                flush_fpc(i)
                out += _const_segment(j - i, v)
            else:
                if fpc_start is None:
                    fpc_start = i
        i = j
    flush_fpc(n)
    return bytes(out)


# ============================================================== model
@dataclass
class Column:
    long_name: str
    values: List[float]


@dataclass
class Sheet:
    name: str
    x: Column
    ys: List[Column]
    contour: bool = False      # EEM sheet: add a virtual matrix + contour graph (Y = column long names)


@dataclass
class Book:
    name: str
    sheets: List[Sheet]


@dataclass
class LineGraph:
    """Overlay of every Y column of one sheet against its X column."""
    book: int = 0
    sheet: int = 0
    name: str = "Graph1"
    y_title: str = "Intensity (a.u.)"
    preview_png: Optional[bytes] = None


# Line colours for the plots after the first (the first keeps the template's dark grey)
PALETTE = [(241, 64, 64), (26, 111, 223), (55, 173, 107), (177, 119, 222), (204, 153, 0),
           (0, 203, 204), (125, 78, 78), (142, 142, 0), (251, 101, 1), (102, 153, 204),
           (111, 184, 2), (81, 81, 81)]   # Origin's default colour list after the first (#515151)


def _julian_now() -> float:
    import time
    return time.time() / 86400.0 + 2440587.5


def _stamp(win_hdr: bytearray) -> None:
    """Window created / modified times (Julian days)."""
    jd = _julian_now()
    struct.pack_into("<dd", win_hdr, 0x73, jd, jd)


def _letters(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _clean(vals) -> List[float]:
    out = []
    for v in vals:
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = float("nan")
        out.append(f if math.isfinite(f) else float("nan"))
    return out


def _nice(lo: float, hi: float) -> Tuple[float, float, float]:
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return 0.0, 1.0, 0.2
    if hi <= lo:
        hi = lo + (abs(lo) or 1.0)
    raw = (hi - lo) / 5
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


# ============================================================== contour support
# Pieces of a contour graph made by OriginPro 2023 from a worksheet (Plot > Contour > Virtual
# Matrix). A virtual matrix is a dataset with no stored data (placeholder blob); Origin rebuilds it
# from the sheet's saved plotvm settings (input range + VM name). Contour levels live in the plot's
# RLE-coded style block and are linearly rescaled to the new data.
_CONTOUR = None
_STYLE_RANGE = ((1127, 1135), (1221, 1229))                 # (low, high) pairs in the decoded style
_STYLE_LEVELS = [1733 + 192 * k for k in range(16)]         # contour levels
_STYLE_TICKS = [4677 + 64 * j for j in range(20)]           # colour-scale ticks
_STYLE_OTHER_LOW = [1541]


def _contour() -> dict:
    global _CONTOUR
    if _CONTOUR is None:
        raw = json.loads(zlib.decompress(base64.b64decode(_CONTOUR_TEMPLATE)))
        _CONTOUR = {"info": raw["info"], **{k: base64.b64decode(v) for k, v in raw["bytes"].items()}}
    return _CONTOUR


def lz4_literal(raw: bytes) -> bytes:
    """A valid LZ4 block made only of literals (no compression)."""
    n = len(raw)
    if n < 15:
        return bytes([n << 4]) + raw
    ext, rest = bytearray(), n - 15
    while rest >= 255:
        ext.append(255)
        rest -= 255
    ext.append(rest)
    return b"\xf0" + bytes(ext) + raw


def _plotvm_storage(book: str, sheet: str, vm: str) -> bytes:
    """The sheet's keyed storage: XFTHEME_PLOTVM (LZ4 XML naming input range and VM) + _SEPD."""
    xml = _contour()["plotvm_xml"]
    xml = xml.replace(b">[Book1]Sheet1<", b">[" + book.encode() + b"]" + sheet.encode("utf-8") + b"<", 1)
    xml = xml.replace(b">VM1<", b">" + vm.encode() + b"<", 1)
    body = struct.pack("<I", len(xml)) + lz4_literal(xml)
    return (b"\x01\x1bXFTHEME_PLOTVM\x04\x01" + leb(len(body)) + b"\x02" + body
            + b"\x09_SEPD\x04\x02\x04\x00\x00\x00\x00\x00\x00")


def _vm_dataset(name: str, cols: List[List[float]], rows: int, obj_id: int) -> bytes:
    C = _contour()
    cells = rows * len(cols)
    h = bytearray(C["vm_hdr"])
    for off in (0x19, 0x21, 0x7F):
        struct.pack_into("<I", h, off, cells)
    flat = [(v if i < len(c) else float("nan")) for c in cols for i, v in
            enumerate(c + [float("nan")] * (rows - len(c)))]
    finite = [(v, i) for i, v in enumerate(flat) if v == v]
    lo = min(finite)[1] if finite else 0
    hi = max(finite)[1] if finite else 0
    struct.pack_into("<II", h, 0x35, lo, hi)                   # cached arg-min / arg-max (column-major)
    nb = name.encode() + b"\0"
    h[0x58:0x71] = nb + bytes(0x71 - 0x58 - len(nb))
    struct.pack_into("<H", h, 0x73, obj_id)
    tail = bytearray(C["vm_tail"])
    struct.pack_into("<H", tail, 79, obj_id - 2)
    assert tail.endswith(b"\x00\x00\x00")
    return rle_block(bytes(h)) + bytes(tail[:-3])   # the 00 00 00 separator is added by the joiner


def _robust_range(vals: List[float]) -> Tuple[float, float]:
    v = sorted(x for x in vals if x == x)
    if not v:
        return 0.0, 1.0
    lo = v[int(0.01 * (len(v) - 1))]
    hi = v[int(0.995 * (len(v) - 1))]
    lo2, hi2, _ = _nice(lo, hi)
    return lo2, hi2


def _contour_graph(name: str, vm_index: int, cells: int, x: List[float], ex: List[float],
                   zvals: List[float], id_base: int, png: bytes, png_h: int) -> bytes:
    C = _contour()
    g = bytearray(C["contour_graph"])
    info = C["info"]
    patches = []

    def struct_at(key):
        a, b = key
        blk = bytes(g[a:b])
        hdr_len = 1 + len(leb(_leb_read(blk, 1)[0]))
        return a, b, bytearray(rle_decode(blk[hdr_len:]))

    a, b, w = struct_at(info["win"])
    nb = name.encode("ascii")[:24]
    w[0x02:0x02 + 25] = nb + bytes(25 - len(nb))
    _stamp(w)
    patches.append((a, b, rle_block(bytes(w))))

    a, b, L = struct_at(info["layer"])
    xs = [v for v in x if v == v]
    struct.pack_into("<dd", L, 0x0F, min(xs), max(xs))
    exs = [v for v in ex if v == v] or [0.0, 1.0]
    struct.pack_into("<dd", L, 0x3A, min(exs), max(exs))
    struct.pack_into("<H", L, 0x101, id_base)
    patches.append((a, b, rle_block(bytes(L))))

    for k, key in enumerate(info["ann"]):
        a, b, A = struct_at(key)
        struct.pack_into("<H", A, 0x67, id_base + 1 + k)
        patches.append((a, b, rle_block(bytes(A))))

    a, b, cur = struct_at(info["curve"])
    struct.pack_into("<H", cur, 0x04, vm_index)
    struct.pack_into("<I", cur, 0x0B, cells)
    struct.pack_into("<H", cur, 0x1E2, id_base + 10)
    patches.append((a, b, rle_block(bytes(cur))))

    for k, key in enumerate(info["axrec"]):
        a, b, R2 = struct_at(key)
        struct.pack_into("<H", R2, 0x1E2, id_base + 11 + k)
        patches.append((a, b, rle_block(bytes(R2))))

    # contour levels: linear map from the template's range onto this sheet's range
    a, b = info["style"]
    blk = bytes(g[a:b])
    n, q = _leb_read(blk, 0)
    sdec = bytearray(rle_decode(blk[q:q + n]))
    old_lo, old_hi = struct.unpack_from("<dd", sdec, _STYLE_RANGE[0][0])
    new_lo, new_hi = _robust_range(zvals)
    scale = (new_hi - new_lo) / (old_hi - old_lo)
    remap = lambda v: new_lo + (v - old_lo) * scale
    for off in [o for pair in _STYLE_RANGE for o in pair] + _STYLE_LEVELS + _STYLE_TICKS + _STYLE_OTHER_LOW:
        v = struct.unpack_from("<d", sdec, off)[0]
        struct.pack_into("<d", sdec, off, remap(v))
    patches.append((a, b, raw_block(rle_encode(bytes(sdec)))))

    out = bytearray()
    cur_pos = 0
    for a, b, new in sorted(patches):
        out += g[cur_pos:a] + new
        cur_pos = b
    out += g[cur_pos:]
    return bytes(out)


# ============================================================== builder pieces
def _embedded_store(long_name: str, short: str, book: str, sheet: str, rows: int) -> bytes:
    """Dataset storage as OriginPro writes it inside an Office-embedded graph: with no workbook
    window in the object, each column carries its own long name, short name, book and sheet,
    plus a series record (from an object OriginPro 2025 pasted into PowerPoint). Without these
    Origin can't rebuild the book when the object is double-clicked."""
    def text(key: bytes, v: str) -> bytes:
        b = v.encode("utf-8")[:250]
        return key + b"\x03" + leb(len(b)) + b
    series = (b"\xe2\x02\x06\x00\x00\x00\x00%_SERIES_OBJECT_MORE\x05\x01\x16\x00"
              b"\x54\x00\x00\x00\x1f\x00\x01\x00\x2c\x17\x09\x0c\x00\x80"
              + struct.pack("<I", rows) + b"\x05\x00")
    return (b"\x01\x0bSYSTEM\x03\x00\x8e\x02\x01"
            + text(b"\xa6\x02", long_name) + text(b"\xb0\x02", short)
            + text(b"\xc8\x02", book) + text(b"\xcc\x02", sheet)
            + series + b"\x00\x00\x00")


def _dataset(kind: str, name: str, vals: List[float], x_index: int,
             store: Optional[bytes] = None) -> bytes:
    P = _pieces()
    n = len(vals)
    h = bytearray(P[f"ds_{kind}_hdr"])
    struct.pack_into("<I", h, 0x19, n)
    struct.pack_into("<I", h, 0x21, n)
    struct.pack_into("<H", h, 0x52, x_index)
    nb = name.encode("ascii") + b"\0"
    if len(nb) > 0x71 - 0x58:
        raise ValueError(f"dataset name too long: {name}")
    h[0x58:0x71] = nb + bytes(0x71 - 0x58 - len(nb))
    struct.pack_into("<H", h, 0x73, 0)                      # no object id (as multi-sheet books)
    tag = P[f"ds_{kind}_tag"]                                # 0A 05 = numeric column record
    body = tag + leb(n) + b"\xff\xff" + leb(n) + b"\0" + encode_segments(vals) + b"\xce"
    # 8-byte content hash: algorithm unknown, Origin accepts any value. Avoid 0x0A so simple
    # pattern-scanning readers (e.g. Principio's) can't mistake it for a record tag.
    h8 = bytes(b if b != 0x0A else 0x0B for b in os.urandom(8))
    blob = b"\x18" + struct.pack("<QQ", 24, len(body)) + h8 + body + P[f"ds_{kind}_tail"]
    return rle_block(bytes(h)) + blob + raw_block(store or P["ds_store"])


def _column_group(tpl: bytes, ds_index: int, x_index: int, rows: int, short: str, long_name: str) -> bytes:
    P = _pieces()
    r = bytearray(tpl)
    struct.pack_into("<H", r, 0x04, ds_index)
    struct.pack_into("<I", r, 0x0B, rows)
    sb = short.encode("ascii")
    r[0x12:0x12 + 8] = sb + bytes(8 - len(sb))
    struct.pack_into("<H", r, 0x23, x_index)
    name = long_name.encode("utf-8")[:250]
    return rle_block(bytes(r)) + P["col_mid"] + raw_block(bytes([len(name) + 1]) + name + b"\0")


def _sheet_fmt(name: str) -> bytes:
    fmt = _pieces()["sheet_fmt"]
    i = fmt.index(b"Sheet1") - 1                              # length-prefixed sheet name
    nb = name.encode("utf-8")[:31]
    return fmt[:i] + bytes([len(nb)]) + nb + fmt[i + 1 + 6:]


def _trailer(last: bool) -> bytes:
    base = b"\x00" * 6 + b"\n" + (b"\x00" * 5 + b"\n") * 4
    return base + (b"\x00" * 6 + b"\n" if last else b"\x00")


def _preview(png: bytes, height: int) -> bytes:
    sub = bytearray(_pieces()["preview_sub"])
    struct.pack_into("<I", sub, 12, height)
    struct.pack_into("<I", sub, 16, struct.unpack(">I", png[16:20])[0])
    payload = struct.pack("<QQQ", 24, 40 + len(png), 0) + bytes(sub) + png
    return raw_block(payload)


def _default_png(xs=None, ys_list=(), kind="line") -> Tuple[bytes, int]:
    """A 200-px-wide thumbnail for the project explorer."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig = plt.figure(figsize=(2.0, 1.55), dpi=100)
        ax = fig.add_axes([0.08, 0.1, 0.88, 0.85])
        if kind == "line" and xs is not None:
            for ys in ys_list:
                ax.plot(xs, ys, lw=0.8)
        elif kind == "grid" and ys_list:
            import numpy as np
            z = np.array(ys_list, dtype=float)
            ax.imshow(z, aspect="auto", origin="lower", cmap="viridis")
        ax.set_xticks([])
        ax.set_yticks([])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100)
        plt.close(fig)
        png = buf.getvalue()
    except Exception:                                         # tiny blank PNG without matplotlib
        png = _blank_png(200, 155)
    return png, struct.unpack(">I", png[20:24])[0]


def _blank_png(w: int, h: int) -> bytes:
    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data))
    raw = b"".join(b"\0" + b"\xff\xff\xff" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _walk(buf: bytes):
    """Minimal block walker for the template's graph part: yields (kind, start, end, payload)."""
    p, n = 0, len(buf)
    while p < n:
        b = buf[p]
        if b == 0:
            if p + 1 < n and buf[p + 1] == 0x0A:
                yield "empty", p, p + 2, b""
                p += 2
            else:
                yield "end", p, p + 1, b""
                p += 1
            continue
        if b == 0x80:
            sz, q = _leb_read(buf, p + 1)
            yield "rle", p, q + sz, rle_decode(buf[q:q + sz])
            p = q + sz
            continue
        if b == 0x81:
            dn, q = _leb_read(buf, p + 1)
            cn, q = _leb_read(buf, q)
            yield "lz4", p, q + cn, buf[q:q + cn]
            p = q + cn
            continue
        sz, q = _leb_read(buf, p)
        yield "raw", p, q + sz + 1, buf[q:q + sz]
        p = q + sz + 1


def _leb_read(buf, p):
    r = s = 0
    while True:
        c = buf[p]
        p += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, p


# ============================================================== public API
# Trailer of a project embedded in an Office document (OLE "Contents" stream), from a graph that
# OriginPro embedded in PowerPoint: project info, an unnamed root folder holding one window, and no
# preview directory.  Embedded projects also have no workbook windows and no window previews, and
# their size / directory header fields are zero.
_EMBEDDED_TAIL = base64.b64decode("SgAAAAqABgTeNhADiYAWAwAAAYoQ2lYpWFDGQkGHtIxaUMZCQQABAAAACgAKBAcBR8ARAQGcCgAEoQFkgAoCdWIKAgCAAYUAAAAA")
ORIGIN_GRAPH_CLSID = bytes.fromhex("b280cc64a14f7b4f9d6f1bfacf5715dc")
ORIGIN_GRAPH_PROGID = "Origin95.Graph"
OLE1_STREAM = bytes.fromhex("0100000200000000000000000000000000000000")


def build(books: Sequence[Book], graph=None,
          project_name: str = "Project", embedded: bool = False) -> bytes:
    """graph: a LineGraph, a list of them (one window each), or None.
    embedded=True: the project form Office documents store for an Origin graph object (the graph
    and its data only).  Needs exactly one graph."""
    P = _pieces()
    graphs = [] if graph is None else (list(graph) if isinstance(graph, (list, tuple)) else [graph])
    if not books:
        raise ValueError("nothing to write")

    # ---- virtual matrices for contour sheets come first in the dataset list
    ds_bytes: List[bytes] = []
    contours = []        # (book index, sheet index, vm name, vm dataset index, cells)
    k = 0
    for bi, book in enumerate(books):
        for si, sh in enumerate(book.sheets):
            if sh.contour and sh.ys:
                k += 1
                rows = max([len(sh.x.values)] + [len(c.values) for c in sh.ys])
                cols = [_clean(c.values) for c in sh.ys]
                ds_bytes.append(_vm_dataset(f"VM{k}", cols, rows, 3000 + 40 * k))
                contours.append((bi, si, f"VM{k}", k, rows * len(cols)))
    vm_of = {(bi, si): name for bi, si, name, _, _ in contours}

    # ---- worksheet columns (numbered after the virtual matrices)
    layout = []          # per book, per sheet: (x_index, [y_index...], rows)
    for bi, book in enumerate(books):
        bl = []
        for si, sh in enumerate(book.sheets):
            suffix = "" if si == 0 else f"@{si + 1}"
            xv = _clean(sh.x.values)
            k += 1
            x_idx = k
            rows_all = max([len(xv)] + [len(c.values) for c in sh.ys])
            meta = (lambda ln, sn: _embedded_store(ln, sn, book.name, sh.name[:31], rows_all)) \
                if embedded else (lambda ln, sn: None)
            ds_bytes.append(_dataset("x", f"{book.name}_A{suffix}", xv, 0, meta(sh.x.long_name, "A")))
            ys = []
            rows = len(xv)
            for ci, col in enumerate(sh.ys):
                yv = _clean(col.values)
                rows = max(rows, len(yv))
                k += 1
                ys.append(k)
                ds_bytes.append(_dataset("y", f"{book.name}_{_letters(ci + 1)}{suffix}", yv, x_idx,
                                         meta(col.long_name, _letters(ci + 1))))
            bl.append((x_idx, ys, rows))
        layout.append(bl)
    out = bytearray(P["header"])
    out += P["ds_sep"].join(ds_bytes)
    out += P["post_ds"]

    previews: List[Tuple[int, int]] = []        # (png offset, png size)
    n_windows = 0

    def add_preview(png: bytes, h: int):
        if embedded:
            return
        blk = _preview(png, h)
        start = len(out) + len(blk) - 1 - len(png)          # PNG is the last thing before "\n"
        out.extend(blk)
        previews.append((start, len(png)))

    # ---- workbook windows (an embedded graph keeps only its data, not the workbook window)
    for bi, book in enumerate(books if not embedded else []):
        wh = bytearray(P["win_hdr"])
        nb = book.name.encode("ascii")[:24]
        wh[0x02:0x02 + 25] = nb + bytes(25 - len(nb))
        wh[0x29] = 0
        _stamp(wh)
        out += rle_block(bytes(wh)) + P["win_rest"]
        for si, sh in enumerate(book.sheets):
            x_idx, ys, rows = layout[bi][si]
            ncols = 1 + len(ys)
            L = bytearray(P["layer"])
            struct.pack_into("<H", L, 0x2B, ncols)
            struct.pack_into("<H", L, 0x151, ncols)
            struct.pack_into("<I", L, 0x52, rows + 1)
            L[0x12D] = si & 0xFF
            struct.pack_into("<H", L, 0x101, 829 + 37 * si)
            out += rle_block(bytes(L)) + raw_block(sh.name.encode("utf-8")[:31])
            mid1 = P["sheet_mid1"]
            if (bi, si) in vm_of:
                sepd = raw_block(b"\x01\x09_SEPD\x04\x02\x04\x00\x00\x00\x00\x00\x00")
                assert sepd in mid1
                mid1 = mid1.replace(sepd, raw_block(_plotvm_storage(book.name, sh.name[:31], vm_of[(bi, si)])), 1)
            out += mid1 + lz4_stored_block(_sheet_fmt(sh.name)) + P["sheet_mid2"]
            groups = [_column_group(P["colrec_x"], x_idx, 0, rows, "A", sh.x.long_name)]
            for ci, (yi, col) in enumerate(zip(ys, sh.ys)):
                groups.append(_column_group(P["colrec_y"], yi, x_idx, rows, _letters(ci + 1), col.long_name))
            out += P["col_end"].join(groups)
            out += _trailer(si == len(book.sheets) - 1)
        sh0 = book.sheets[0]
        png, h = _default_png(_clean(sh0.x.values), [_clean(c.values) for c in sh0.ys[:12]])
        add_preview(png, h)
        n_windows += 1

    # ---- contour graphs (one per contour sheet)
    for n, (bi, si, vm, vm_idx, cells) in enumerate(contours):
        sh = books[bi].sheets[si]
        ex = []
        for c in sh.ys:
            try:
                ex.append(float(c.long_name))
            except ValueError:
                ex.append(float("nan"))
        zvals = [v for c in sh.ys for v in _clean(c.values)]
        rows = cells // max(1, len(sh.ys))
        grid = [(_clean(c.values) + [float("nan")] * rows)[:rows] for c in sh.ys]
        png, h = _default_png(None, grid, kind="grid")
        gname = f"Contour{n + 1}"
        out += _contour_graph(gname, vm_idx, cells, _clean(sh.x.values), ex, zvals, 3500 + 40 * n, png, h)
        add_preview(png, h)
        n_windows += 1

    # ---- graph windows (one per LineGraph)
    for graph in graphs:
        sh = books[graph.book].sheets[graph.sheet]
        x_idx, ys, rows = layout[graph.book][graph.sheet]
        head = P["graph_head"]
        parts, layer_done = [], False
        xs = [v for v in _clean(sh.x.values) if v == v]
        yall = [v for c in sh.ys for v in _clean(c.values) if v == v]
        for kind, a, b, pay in _walk(head):
            seg = head[a:b]
            if kind == "rle" and len(pay) == 195 and not parts:
                g = bytearray(pay)
                nb = graph.name.encode("ascii")[:24]
                g[0x02:0x02 + 25] = nb + bytes(25 - len(nb))
                _stamp(g)
                seg = rle_block(bytes(g))
            elif kind == "rle" and len(pay) == 405 and not layer_done:
                g = bytearray(pay)
                for off, vals in ((0x0F, xs), (0x3A, yall)):
                    lo, hi, st = _nice(min(vals), max(vals)) if vals else (0, 1, 0.2)
                    if off == 0x0F and vals:
                        lo, hi = min(vals), max(vals)      # X hugs the data, as Origin does
                    struct.pack_into("<ddd", g, off, lo, hi, st)
                    g[off + 0x1C] = max(1, min(255, math.ceil((hi - lo) / st - 1e-9) + 1))
                seg = rle_block(bytes(g))
                layer_done = True
            elif kind == "raw" and pay.startswith(b"\\l(1)"):
                lines = "\r\n".join(f"\\l({i + 1}) %({i + 1})" for i in range(len(ys)))
                seg = safe_text_block(lines)
            elif kind == "raw" and pay == b"Intensity (a.u.)\x00":
                seg = safe_text_block(graph.y_title)
            parts.append(seg)
        out += b"".join(parts)
        for i, yi in enumerate(ys):
            r = bytearray(P["curve_lead"] if i == 0 else P["curve_memb"])
            struct.pack_into("<H", r, 0x04, yi)
            struct.pack_into("<H", r, 0x23, x_idx)
            struct.pack_into("<I", r, 0x0B, rows)
            r[0x27:0x2F] = bytes(8)
            if i:
                c = PALETTE[(i - 1) % len(PALETTE)]
                r[0x16A:0x16E] = bytes([c[0], c[1], c[2], 1])
            out += rle_block(bytes(r)) + P["curve_mid"]
            out += raw_block(P["style_lead"] if i == 0 else P["style_memb"]) + P["curve_end"]
        out += P["graph_tail"]
        png, h = (graph.preview_png, struct.unpack(">I", graph.preview_png[20:24])[0]) \
            if graph.preview_png else _default_png(_clean(sh.x.values), [_clean(c.values) for c in sh.ys])
        add_preview(png, h)
        n_windows += 1

    if embedded:
        if n_windows != 1:
            raise ValueError("an embedded project holds exactly one graph")
        aw = P["after_windows"]
        out += aw[:aw.index(b"\xcf\x00\x00\x00\n")] + _EMBEDDED_TAIL + P["post_dir"]
        struct.pack_into("<Q", out, 0x42, 0)
        struct.pack_into("<Q", out, 0xC2, 0)
        return bytes(out)

    # ---- notes, project tree, preview directory, project storage
    out += P["after_windows"]
    tree = P["tree_prefix"].replace(
        struct.pack("<I", len(P["project_name"]) + 1) + b"\n" + P["project_name"] + b"\0",
        struct.pack("<I", len(project_name.encode("utf-8")) + 1) + b"\n" + project_name.encode("utf-8") + b"\0", 1)
    out += tree + bytes([2 * n_windows]) + b"\x00"
    for i in range(n_windows):
        out += rle_block(struct.pack("<II", 0, i)) + b"\x00\x00"
    out += b"\x00\x00"
    dir_off = len(out)
    out += struct.pack("<QII", len(previews), 0, 16 * len(previews))
    for o, s in previews:
        out += struct.pack("<QQ", o, s)
    out += P["post_dir"]
    struct.pack_into("<Q", out, 0x42, dir_off)
    struct.pack_into("<Q", out, 0xC2, len(out))
    return bytes(out)


def save(path, books, graph=None, project_name=None) -> None:
    """graph: a LineGraph, a list of LineGraphs, or None."""
    path = Path(path)
    data = build(books, graph, project_name or path.stem)
    path.write_bytes(data)


# Pieces of a project saved by OriginPro 2023 (see extract notes at the top), zlib + base64.
_TEMPLATE = (
    "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"
    "fN77fHT0sZsBg7AU+AsyisWsN5sJPTJRwWXemGsfJ/LOlPOB7VNoKyIVdjjGBesW/I+5MEfnwvj80aDtCn+hnCTq7qEsUG9o"
    "qk/LX6/Ph9m31SULmmf9LumT98WDH2371IsH80w+sifRMz8thcXZD7K9vnrf+eSJizfAoJ0/lSvuf9jR9tUzwZa2FRt5fdcb"
    "LLIa3+30Igvz1c4svW33en/wxTZVyS7nr8+j/q+XjPX5Kz8F9fVdZJl+8Lyuv5Yvwf8HMvU8jHqXrVcB+FYfF3xoznmXqfi8"
    "zP3dT1k/4skyjOz3tv9+db3m6fazX1w80Fq8nq3H71rsX3dF/ZQW/6h27x8t/l+oxdZRXnOfX2rx5/2Xf7UWX67/e1qcfFjx"
    "yk9+Wgbbv5xWN1a7lzBzqCy4vSOayzHn22V6GYKNSDa1yzwDVhqYZxLBxxWXsuN0nuuttjRX0twDH2yor4V0baKvYQ7tYB5Z"
    "KXMhyScq3voIFsNM9sl/uYkVz9q/ubkf86tJsRbP49wA1wt1tG2xJGR5Dq/qaX/lO//lJlacYMyllOPQ92t/Jmt7dTbVeztH"
    "62FaUHL/7rNIPbXaeF7WM/ijN74VxYiHxz35fbaeR3sGrUWxfI0K6pzH4xSrG9oH+/v8m3wt7X+Hj9PIfZxqpOMsHaNqa+rS"
    "eoxigwKNmwliubj6di1e6GvwW6lthlCEUcypXyii5zyUOkqDNt+i+LSRE6WNgnCDfE0RonRQuvlLSKKH0kcZXOp+yO9zrKtP"
    "utC5F6rzr+67y7rnX5xXoLobMeVg/nLZP/8tMu6W/1oZU04iTrmHc6le+9bxnlywsaAtNQlvu6wDfoZN4sfUL+B40tG5YU91"
    "k0zwrRQCMkMs2UtdGCedj8n8SPp35pmRjY1vrPybV/OPlf/Hyv9j5anu2t9iaejZv8Oae3+LNd/9PTPm698i4/Y2v09dVwiG"
    "3l9m5fN9aC+jZHXzXmnjelTkyWCkmpXIpRdOKMZZbRnAVXnCFik/7c0x7/SsygTcFq2IWd6BUD/D4lUqdNzSuk6ez5TcrD+x"
    "dobeV6K5kH5XN0VrIIgsWaw8O4uf2XPaW8kCIWyxEmOnJvVem/J+bTo+sbfcKM00IyX8K49mjfK3tDAuY1Y8FJ6zoGCakWVy"
    "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"
    "TBXdnss3VxTev9R++uLG08cb/9//B0IG9fE="
)


# Contour graph / virtual matrix pieces from a project saved by OriginPro 2023.
_CONTOUR_TEMPLATE = (
    "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"
    "NWbv18iUimun3kgbTmv08BjiIThnisccD0x7bCveu8KjgscTHiUoegs+3u/j/T7eL97I8/F+8a6q/8Jw7hCPBOf1Nf7hvyXE"
    "nzV4oq/HaSthcUfs2xGXHoe26HdZmqWSDYcXkqRGklM+Yy4DZyqJPqDob8w4fj7x63tXQK35Ep5RHsRqa8DeEUuJXhWmLpVM"
    "IZnDyII6CwYkiollyXoAurUTPQzRL1EU8bm89qMY0ew1+OwiY6sqel8UKZyIdw2fdsLGNmsoNPahRmeW/y5nW8hZ+KV4B6pV"
    "+MNzKTvN3c092dFxTiFv6SxvQ9iAlBTt3O8zjSER/yZgC1vmM/EfPxY+phedsXfZiz5PqfRZZod/lhmxHSwvOi93572EuH96"
    "2qtKq6L3VbzHJnpoQ65HhEYW0ZRY5I3GM7fO7+tFzUD825qubFBO6x+w/FKeB+2t56TMiUXegP732JurMNmgoKMysR63dwd+"
    "sLd44dKQnqWSJJUmuA+zmaPg/JYHKriP29x6yOZefrK5BOr/CZubJ7nIa4GTwc7jJcj1y9pTTpbFujBUxbwkaJ3G7FDzi72X"
    "S1iccDMN2E1Q9vN8ivUrvf735z7z3g3//fvv//zjt9+ni8my+B928+kCP/+U//itXKn9A69kw8OL+B93/6wo1T9+U5Sq+DLe"
    "vu5exJdPlXL5j9+elMqT+HrzdshOXyuV5z9+q6tKWXw9XBRz/vn8jNOqVQW/+1Ot4331mrjtz3LtqYLrPVXU4uzpuYZnz0ox"
    "rlKu40yVyrP8j2Kq/etLXExWrz/jhbIsK6e75KpSF+dKpfqPf/zzn/8FCA/bHg=="
)
