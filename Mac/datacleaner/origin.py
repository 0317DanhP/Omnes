"""Origin project (.opju) output, written directly by opju_build (no Origin needed)."""
from __future__ import annotations

from .core import *  # noqa: F401,F403

def save_opju(path: Path, tables: list, log, contour: bool = False) -> bool:
    """Write an Origin project with opju_build (no Origin needed):
      book 'Spectra': one sheet per file / instrument group (Wavelength (nm) + one column per
                      sample), each with its own overlay line graph
      book 'EEM':     one sheet per sample (Wavelength (nm), 650, 645, ...); with contour=True
                      each sheet also gets a contour graph (EXPERIMENTAL: the graphs open in
                      OriginPro but don't show data yet)"""
    from . import opju_build as OB
    used: set[str] = set()

    def sheet(name: str, df: pd.DataFrame, contour: bool = False):
        x = OB.Column(str(df.index.name or WL), [float(v) for v in df.index])
        ys = [OB.Column(str(c), df[c].to_numpy(dtype=float).tolist()) for c in df.columns]
        return OB.Sheet(_sheet_name(name, used), x, ys, contour=contour)

    books, graphs = [], []
    spectra = [(n, df) for n, df, kind in tables if kind == "group"]
    eems = [(n, df) for n, df, kind in tables if kind == "eem"]
    if spectra:
        books.append(OB.Book("Spectra", [sheet(n, df) for n, df in spectra]))
        gnames: set = {"Spectra", "EEM"} | {f"Contour{k + 1}" for k in range(len(eems))}
        for i, (n, _) in enumerate(spectra):
            g = re.sub(r"[^A-Za-z0-9]", "", n)[:20] or "Graph"
            if not g[0].isalpha():
                g = "G" + g[:19]
            while g in gnames:
                g = g[:18] + str(i + 1)
            gnames.add(g)
            graphs.append(OB.LineGraph(book=0, sheet=i, name=g))
    if eems:
        books.append(OB.Book("EEM", [sheet(n, df, contour=contour) for n, df in eems]))
    if not books:
        return False
    try:
        OB.save(path, books, graphs or None, project_name=path.stem)
        return True
    except Exception as e:
        log(f"ERROR  Origin project {path.name}: {e}")
        return False



__all__ = [n for n in dir() if not n.startswith("__")]
