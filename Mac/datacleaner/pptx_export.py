"""PowerPoint export: one slide per graph, with Origin graphs embedded as editable OLE objects.

Each Origin graph is written as the project form Office keeps inside an embedded Origin graph object
(opju_build(..., embedded=True)), wrapped in an OLE compound file with Origin's graph CLSID, exactly
as OriginPro embeds graphs.  Double-clicking it in PowerPoint opens it in Origin, and Principio's
PowerPoint add-in can edit it.  The slide shows a rendered preview until Origin refreshes it.
The compound-file writer comes from Principio (ppt_origin.py).
"""
from __future__ import annotations

import io
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import opju_build as OB
from .core import WL, _sheet_name

# ============================================================== OLE compound files (from Principio)
MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
FREESECT, ENDOFCHAIN, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF




@dataclass
class CFBEntry:
    name: str
    kind: int                  # 1 storage, 2 stream, 5 root
    data: bytes = b""
    clsid: bytes = bytes(16)
    state: int = 0
    children: List["CFBEntry"] = field(default_factory=list)



def _cfb_key(name: str):
    return (len(name), name.upper())


def cfb_write(root: CFBEntry) -> bytes:
    """Serialise an entry tree as a version-3 compound file (512-byte sectors)."""
    SS, MSS, CUTOFF = 512, 64, 4096
    entries: List[CFBEntry] = []
    ids: Dict[int, int] = {}

    def collect(e):
        ids[id(e)] = len(entries)
        entries.append(e)
        for c in e.children:
            collect(c)

    collect(root)
    # mini stream
    mini = bytearray()
    minifat: List[int] = []
    start_of: Dict[int, int] = {}
    big: List[CFBEntry] = []
    for e in entries:
        if e.kind != 2:
            continue
        if len(e.data) < CUTOFF:
            if not e.data:
                start_of[id(e)] = ENDOFCHAIN
                continue
            first = len(mini) // MSS
            n = -(-len(e.data) // MSS)
            mini += e.data + bytes(n * MSS - len(e.data))
            minifat += list(range(first + 1, first + n)) + [ENDOFCHAIN]
            start_of[id(e)] = first
        else:
            big.append(e)
    sectors: List[bytes] = []
    fat: List[int] = []

    def alloc(data: bytes, pad_to=SS) -> int:
        if not data:
            return ENDOFCHAIN
        n = -(-len(data) // SS)
        first = len(sectors)
        data = bytes(data) + bytes(n * SS - len(data))
        for k in range(n):
            sectors.append(data[k * SS:(k + 1) * SS])
            fat.append(first + k + 1 if k + 1 < n else ENDOFCHAIN)
        return first

    for e in big:
        start_of[id(e)] = alloc(e.data)
    mini_start = alloc(bytes(mini))
    mf_bytes = struct.pack(f"<{len(minifat)}I", *minifat) if minifat else b""
    if mf_bytes and len(mf_bytes) % SS:
        mf_bytes += struct.pack("<I", FREESECT) * ((SS - len(mf_bytes) % SS) // 4)
    mf_start = alloc(mf_bytes)
    n_mf_sect = len(mf_bytes) // SS

    # directory: red-black tree with every node black, balanced by CFB name order
    left = [NOSTREAM] * len(entries)
    right = [NOSTREAM] * len(entries)
    child = [NOSTREAM] * len(entries)

    def balance(items):
        if not items:
            return NOSTREAM
        m = len(items) // 2
        i = ids[id(items[m])]
        left[i] = balance(items[:m])
        right[i] = balance(items[m + 1:])
        return i

    for e in entries:
        if e.kind in (1, 5):
            child[ids[id(e)]] = balance(sorted(e.children, key=lambda c: _cfb_key(c.name)))
    dirb = bytearray()
    for i, e in enumerate(entries):
        nm = e.name.encode("utf-16-le")[:62]
        ent = bytearray(128)
        ent[:len(nm)] = nm
        struct.pack_into("<H", ent, 64, len(nm) + 2)
        ent[66] = e.kind
        ent[67] = 1                                     # black
        struct.pack_into("<III", ent, 68, left[i], right[i], child[i])
        ent[80:96] = e.clsid if e.kind != 2 else bytes(16)
        struct.pack_into("<I", ent, 96, e.state)
        if e.kind == 2:
            struct.pack_into("<IQ", ent, 116, start_of[id(e)], len(e.data))
        elif e.kind == 5:
            struct.pack_into("<IQ", ent, 116, mini_start if mini else ENDOFCHAIN, len(mini))
        else:
            struct.pack_into("<IQ", ent, 116, 0, 0)
        dirb += ent
    while len(dirb) % SS:
        empty = bytearray(128)
        struct.pack_into("<III", empty, 68, NOSTREAM, NOSTREAM, NOSTREAM)
        dirb += empty
    dir_start = alloc(bytes(dirb))

    # FAT (+ DIFAT for very large files)
    per = SS // 4
    n_fat = 1
    while True:
        n_dif = 0 if n_fat <= 109 else -(-(n_fat - 109) // (per - 1))
        total = len(sectors) + n_fat + n_dif
        if -(-total // per) <= n_fat:
            break
        n_fat += 1
    fat_first = len(sectors)
    fat += [FATSECT] * n_fat + [DIFSECT] * n_dif
    dif_first = fat_first + n_fat
    fat += [FREESECT] * (n_fat * per - len(fat))
    fat_bytes = struct.pack(f"<{len(fat)}I", *fat)
    for k in range(n_fat):
        sectors.append(fat_bytes[k * SS:(k + 1) * SS])
    fat_sects = list(range(fat_first, fat_first + n_fat))
    rest = fat_sects[109:]
    for k in range(n_dif):
        chunk = rest[k * (per - 1):(k + 1) * (per - 1)]
        chunk += [FREESECT] * (per - 1 - len(chunk))
        nxt = dif_first + k + 1 if k + 1 < n_dif else ENDOFCHAIN
        sectors.append(struct.pack(f"<{per}I", *chunk, nxt))

    hdr = bytearray(512)
    hdr[:8] = MAGIC
    struct.pack_into("<HHHHH", hdr, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<I", hdr, 0x2C, n_fat)
    struct.pack_into("<I", hdr, 0x30, dir_start)
    struct.pack_into("<IIIII", hdr, 0x38, CUTOFF, mf_start if mf_bytes else ENDOFCHAIN, n_mf_sect,
                     dif_first if n_dif else ENDOFCHAIN, n_dif)
    difat = fat_sects[:109] + [FREESECT] * (109 - min(109, n_fat))
    struct.pack_into("<109I", hdr, 0x4C, *difat)
    return bytes(hdr) + b"".join(sectors)




def origin_ole(contents: bytes) -> bytes:
    """An embedded Origin graph object: Root (Origin graph CLSID) > \\x01Ole, Contents (.opju)."""
    root = CFBEntry("Root Entry", 5, clsid=OB.ORIGIN_GRAPH_CLSID, children=[
        CFBEntry("\x01Ole", 2, OB.OLE1_STREAM),
        CFBEntry("Contents", 2, contents),
    ])
    return cfb_write(root)


# ============================================================== previews
# Origin's default graph page (what "Send Graphs to PowerPoint" / copy-paste produce):
# 10.72 x 8.205 in, layer from 17.8% to 86% across and 11% to 82.8% down, Arial 22 pt,
# thin 0.5 pt lines in Origin's default colour list, box frame, outward major + minor ticks.
PAGE_EMU = (9802224, 7502316)
PAGE_IN = (PAGE_EMU[0] / 914400, PAGE_EMU[1] / 914400)
LAYER = (0.178, 0.172, 0.860 - 0.178, 0.828 - 0.172)     # left, bottom, width, height (fig fraction)
ORIGIN_COLORS = ["#515151", "#F14040", "#1A6FDF", "#37AD6B", "#B177DE",
                 "#CC9900", "#00CBCC", "#7D4E4E", "#8E8E00", "#FB6501", "#6699CC", "#6FB802"]


def _origin_axes(ax, xlabel, ylabel):
    from matplotlib.ticker import AutoMinorLocator
    ax.set_xlabel(xlabel, fontsize=22)
    ax.set_ylabel(ylabel, fontsize=22)
    ax.tick_params(which="both", direction="out", top=False, right=False, labelsize=22,
                   width=1.0, color="black")
    ax.tick_params(which="major", length=8)
    ax.tick_params(which="minor", length=4)
    ax.xaxis.set_minor_locator(AutoMinorLocator(2))
    ax.yaxis.set_minor_locator(AutoMinorLocator(2))
    for sp in ax.spines.values():
        sp.set_linewidth(1.0)
        sp.set_color("black")


def _fig_png(draw, size=PAGE_IN, rect=LAYER, dpi=110) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    have = {f.name for f in font_manager.fontManager.ttflist}
    family = next((f for f in ("Arial", "Liberation Sans", "Helvetica", "Arimo") if f in have), "DejaVu Sans")
    with plt.rc_context({"font.family": family,
                         "axes.autolimit_mode": "round_numbers", "axes.xmargin": 0,
                         "axes.ymargin": 0.05}):
        fig = plt.figure(figsize=size, dpi=dpi)
        ax = fig.add_axes(rect)
        draw(fig, ax)
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
    return buf.getvalue()


def line_png(df, y_title: str = "Intensity (a.u.)") -> bytes:
    def draw(fig, ax):
        for i, c in enumerate(df.columns):
            s = df[c].dropna()
            ax.plot(s.index, s.values, lw=0.6, color=ORIGIN_COLORS[i % len(ORIGIN_COLORS)], label=str(c))
        xs = df.index.to_numpy(dtype=float)
        if len(xs):
            ax.set_xlim(xs.min(), xs.max())
        _origin_axes(ax, df.index.name or WL, y_title)
        if df.shape[1] <= 15:
            ax.legend(frameon=False, fontsize=22 if df.shape[1] <= 10 else 14, loc="upper right",
                      handlelength=2.6, borderaxespad=0.3, labelspacing=0.2)
    return _fig_png(draw)


def eem_png(df, title: str) -> bytes:
    import numpy as np

    def draw(fig, ax):
        ex = np.array([float(c) for c in df.columns])
        order = np.argsort(ex)
        z = df.to_numpy(dtype=float)[:, order]
        em = df.index.to_numpy(dtype=float)
        # colour range from the fluorescence only: leave out Rayleigh scatter (em ~ ex) and its
        # second order (em ~ 2 ex), which saturate the detector and would wash out the scale
        E, X = np.meshgrid(em, ex[order], indexing="ij")
        clean = np.isfinite(z) & (np.abs(E - X) > 25) & (np.abs(E - 2 * X) > 25)
        vals = z[clean] if clean.sum() > 20 else z[np.isfinite(z)]
        lo, hi = (np.percentile(vals, 1), np.percentile(vals, 99.5)) if vals.size else (0, 1)
        if hi <= lo:
            hi = lo + 1
        cs = ax.contourf(em, ex[order], z.T, levels=np.linspace(lo, hi, 17), cmap="jet", extend="both")
        cax = fig.add_axes([LAYER[0] + LAYER[2] + 0.015, LAYER[1], 0.025, LAYER[3]])
        cb = fig.colorbar(cs, cax=cax)
        cb.ax.tick_params(labelsize=14)
        _origin_axes(ax, "Emission Wavelength (nm)", "Excitation Wavelength (nm)")
        ax.set_title(title, fontsize=22)
    return _fig_png(draw, rect=(0.15, LAYER[1], 0.66, LAYER[3]))


# ============================================================== deck
def _origin_frame(shape) -> None:
    """python-pptx embeds objects as icons; make it a normal Origin graph object like Origin's own."""
    el = shape._element
    for ole in el.iter("{http://schemas.openxmlformats.org/presentationml/2006/main}oleObj"):
        if "showAsIcon" in ole.attrib:
            del ole.attrib["showAsIcon"]
        ole.set("name", "Graph")
        ole.set("imgW", str(PAGE_EMU[0]))
        ole.set("imgH", str(PAGE_EMU[1]))
    _lock_aspect(shape)


def _lock_aspect(shape) -> None:
    from pptx.oxml.ns import qn
    el = shape._element
    for tag in ("p:cNvGraphicFramePr", "p:cNvPicPr"):
        for pr in el.iter(qn(tag)):
            locks = pr.find(qn("a:graphicFrameLocks" if tag == "p:cNvGraphicFramePr" else "a:picLocks"))
            if locks is None:
                locks = pr.makeelement(qn("a:graphicFrameLocks" if tag == "p:cNvGraphicFramePr"
                                          else "a:picLocks"), {})
                pr.insert(0, locks)
            locks.attrib.pop("noGrp", None)
            locks.set("noChangeAspect", "1")


def export_pptx(path: Path, decks: list, log, contour: bool = False) -> bool:
    """decks: [(title, tables)] where tables come from pipeline.plan_tables.

    Each graph gets its own slide laid out like OriginPro's copy/paste into PowerPoint: the graph
    page at its own aspect ratio, centred, 79% of the slide height, no extra title box (the shape
    is named after the sheet so it shows in the Selection Pane)."""
    try:
        from pptx import Presentation
        from pptx.util import Emu, Inches
    except ImportError:
        log("PowerPoint skipped: install python-pptx (pip install python-pptx)")
        return False
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
    blank = prs.slide_layouts[6]
    n_obj = n_pic = 0
    height = Emu(5418137)
    width = Emu(round(height * PAGE_EMU[0] / PAGE_EMU[1]))
    left = Emu((prs.slide_width - width) // 2)
    top = Emu((prs.slide_height - height) // 2)

    def add_graph(label, proj, png):
        s = prs.slides.add_slide(blank)
        if proj is None:
            shp = s.shapes.add_picture(io.BytesIO(png), left, top, width, height)
            _lock_aspect(shp)
        else:
            shp = s.shapes.add_ole_object(io.BytesIO(origin_ole(proj)), OB.ORIGIN_GRAPH_PROGID,
                                          left, top, width, height, icon_file=io.BytesIO(png),
                                          icon_width=width, icon_height=height)
            _origin_frame(shp)
        shp.name = label[:250]
        return shp

    for title, tables in decks:
        used: set = set()
        for name, df, kind in tables:
            if kind in ("all", "group"):
                sheet_name = _sheet_name(name if kind == "group" else "All samples", used)
                sheet = OB.Sheet(sheet_name, OB.Column(str(df.index.name or WL),
                                 [float(v) for v in df.index]),
                                 [OB.Column(str(c), df[c].to_numpy(dtype=float).tolist()) for c in df.columns])
                proj = OB.build([OB.Book("Spectra", [sheet])], OB.LineGraph(sheet=0), title, embedded=True)
                add_graph(f"{title}: {sheet_name}", proj, line_png(df))
                n_obj += 1
            elif kind == "eem":
                png = eem_png(df, name)
                if contour:
                    sheet = OB.Sheet(_sheet_name(name, used), OB.Column(str(df.index.name or WL),
                                     [float(v) for v in df.index]),
                                     [OB.Column(str(c), df[c].to_numpy(dtype=float).tolist()) for c in df.columns],
                                     contour=True)
                    add_graph(f"{title}: {name}", OB.build([OB.Book("EEM", [sheet])], None, name, embedded=True), png)
                    n_obj += 1
                else:
                    add_graph(f"{title}: {name}", None, png)
                    n_pic += 1
    if not len(prs.slides):
        log("PowerPoint skipped: no graphs to export")
        return False
    prs.save(str(path))
    log(f"Wrote {path.name}: {len(prs.slides)} slide(s) "
        f"({n_obj} Origin graph object(s), {n_pic} picture(s))")
    if n_obj:
        log("       warning: double-clicking these graphs in PowerPoint doesn't open them in Origin "
            "yet; Principio's PowerPoint add-in can open them")
    return True
