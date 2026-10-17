"""Folder scanning, alignment/averaging, workbook planning and writing."""
from __future__ import annotations

from .core import *  # noqa: F401,F403
from .detect import *  # noqa: F401,F403
import os

from .origin import save_opju  # noqa: F401

# ================================================================ processing

@dataclass
class Parsed:
    path: Path
    fmt: str
    spectra: list
    eems: list


def _is_companion(p: Path) -> bool:
    return (p.name.lower() in EZSPEC_COMPANIONS
            or p.suffix.lower() in EZSPEC_COMPANION_SUFFIXES)


def scan_folder(folder: Path, force: Optional[str] = None, log=print,
                recursive: bool = False) -> list[Parsed]:
    folder = Path(folder)
    out_dir = folder / OUTPUT_DIR
    paths = folder.rglob("*") if recursive else folder.iterdir()
    return scan_paths([x for x in paths if x.is_file() and out_dir not in x.parents], folder, force, log)


def scan_paths(paths, base: Path, force: Optional[str] = None, log=print,
               picked: bool = False) -> list[Parsed]:
    """Read every file in paths. picked=True: the user chose these files one by one, so an
    EzSpec companion file (Signals.ezspec_data, *.ezspec_raw) is read rather than skipped."""
    base = Path(base)
    results, ignored, companions = [], 0, 0
    for p in sorted(Path(x) for x in paths):
        try:
            rel = p.relative_to(base).as_posix()
        except ValueError:
            rel = p.name
        if _is_companion(p) and not (force or picked):
            companions += 1
            continue
        if p.name.startswith((".", "~$")) or p.suffix.lower() in SKIP_SUFFIXES:
            ignored += 1
            continue
        binary = looks_binary(p)
        if binary and not (is_fbsw(p) or force):
            ignored += 1
            continue
        try:
            text = "" if binary else read_text(p)
            key = force or detect(p, text)
            if key is None:
                log(f"SKIP   {rel}: format not recognized")
                continue
            items = FORMATS[key].parse(p, text)
            spectra = [s for s in items if isinstance(s, pd.Series) and len(s) >= MIN_POINTS]
            eems = [e for e in items if isinstance(e, EEM) and not e.matrix.empty]
            if not spectra and not eems:
                log(f"SKIP   {rel}: no data found (read as {FORMATS[key].label})")
                continue
            results.append(Parsed(p, key, spectra, eems))
            what = []
            if spectra:
                what.append(f"{len(spectra)} spectrum(s)")
            for e in eems:
                ex = e.matrix.columns
                what.append(f"EEM {len(e.matrix)} em x {len(ex)} ex ({ex[0]}-{ex[-1]} nm)")
                if e.extras:
                    what.append("+ " + ", ".join(e.extras))
            log(f"OK     {rel}: {FORMATS[key].label}, " + " ".join(what))
            for e in eems:
                if e.info.get("dbl.Saturation") == "true":
                    log("       note: EzSpec flagged detector saturation in this acquisition")
                err = _check_correction(e)
                if err is not None:
                    log(f"       check: EEM recomputed from raw signals matches "
                        f"(median deviation {err:.2g}%)")
        except Exception as e:
            log(f"ERROR  {rel}: {e}")
    if companions:
        log(f"({companions} EzSpec session file(s) read as part of their session)")
    if ignored:
        log(f"({ignored} non-data file(s) ignored)")

    # The same EEM can arrive more than once (EzSpec session + .dat export, .FBSW + .csv export).
    # Keep the most complete copy: EzSpec session > FBSW > CSV > DAT.
    best: dict[str, EEM] = {}
    for r in results:
        for e in r.eems:
            cur = best.get(e.name.lower())
            if cur is None or EEM_SOURCE_RANK.get(e.source, 9) < EEM_SOURCE_RANK.get(cur.source, 9):
                best[e.name.lower()] = e
    for r in list(results):
        dupes = [e for e in r.eems if best[e.name.lower()] is not e]
        if dupes:
            r.eems = [e for e in r.eems if e not in dupes]
            for e in dupes:
                log(f"       {r.path.name}: EEM '{e.name}' also found as {best[e.name.lower()].source}"
                    f"; using that copy")
            if not r.eems and not r.spectra:
                results.remove(r)

    # Identical spectra exported twice (e.g. the .FBSW and its .csv): keep one.
    kept: dict[str, list] = {}
    for r in list(results):
        keep = []
        for sp in r.spectra:
            twin = next((k for k in kept.get(sp.name, [])
                         if len(k) == len(sp) and np.allclose(k.index, sp.index)
                         and np.allclose(k.values, sp.values, rtol=1e-6, equal_nan=True)), None)
            if twin is None:
                kept.setdefault(sp.name, []).append(sp)
                keep.append(sp)
        if len(keep) < len(r.spectra):
            log(f"       {r.path.name}: {len(r.spectra) - len(keep)} spectrum(s) identical to "
                f"another file's; skipped")
            r.spectra = keep
            if not r.eems and not r.spectra:
                results.remove(r)
    return results


def combine(spectra: list, interpolate: bool = False, average: bool = False) -> pd.DataFrame:
    """Align spectra on wavelength (union of all grids). Missing points stay blank unless
    interpolate=True, which fills them linearly inside each sample's own range only.
    average=True first collapses each spectrum onto integer wavelengths."""
    if average:
        spectra = [average_to_integers(s) for s in spectra]
    seen: dict[str, int] = {}
    cols = []
    for s in spectra:
        name = s.name or "Sample"
        n = seen.get(name, 0)
        seen[name] = n + 1
        cols.append(s.rename(name if n == 0 else f"{name} ({n + 1})"))
    df = pd.concat(cols, axis=1, join="outer").sort_index()
    if interpolate:
        df = df.interpolate(method="index", limit_area="inside")
    df.index.name = WL
    return df


def _report_gaps(df: pd.DataFrame, interpolate: bool, log) -> None:
    gaps = int(df.isna().any(axis=1).sum())
    if gaps and df.shape[1] > 1:
        where = " (outside some samples' ranges)" if interpolate else ""
        log(f"       note: {gaps} of {len(df)} wavelengths lack a value in every column{where}; "
            f"those cells are blank")




def _eem_tables(eem: EEM, average: bool) -> list[tuple[str, pd.DataFrame]]:
    m = average_to_integers(eem.matrix) if average else eem.matrix
    tables = [("EEM", m)]
    for name, df in eem.extras.items():
        tables.append((name, average_to_integers(df) if average and name in eem.em_indexed else df))
    if eem.info:
        tables.append(("Info", pd.Series(eem.info, name="Value").rename_axis("Setting").to_frame()))
    return tables


# Formats whose files each hold one spectrum: every file of that kind goes into one shared sheet.
# Anything that holds several spectra per file (a Cary CSV, an FBSW batch) gets a sheet of its own.
GROUP_NAMES = {
    "ocean": "Ocean Optics", "jcamp": "Agilent DAD", "ezspec": "Duetta", "duetta_txt": "Duetta",
    "dat": "FT-300", "k2": "K2 ISS", "generic": "Other", "cary": "Cary", "fbsw": "Cary Eclipse",
}
PER_FILE = {"cary", "fbsw"}


def group_spectra(results: list) -> list[tuple[str, list]]:
    """[(sheet name, spectra)] in file order: one group per multi-spectrum file, and one group per
    instrument for files that each hold a single spectrum."""
    groups: dict[str, list] = {}
    for r in results:
        if not r.spectra:
            continue
        if r.fmt in PER_FILE or len(r.spectra) > 1:
            key = r.path.stem
            while key in groups:
                key += "+"
        else:
            key = "\0" + GROUP_NAMES.get(r.fmt, r.fmt)
        groups.setdefault(key, []).extend(r.spectra)
    return [(k.lstrip("\0"), v) for k, v in groups.items()]


def plan_tables(results: list, interpolate: bool, average: bool, log
                ) -> list[tuple[str, pd.DataFrame, str]]:
    """Sheets for one workbook: (sheet name, table, kind).
    kind: 'group' = the spectra of one file (or of all single-spectrum files from one instrument)
          aligned on wavelength, 'eem' = one sample's EEM, 'extra' = raw signals / settings that go
          with an EEM."""
    tables = []
    for name, spectra in group_spectra(results):
        df = combine(spectra, interpolate, average)
        tables.append((name, df, "group"))
        _report_gaps(df, interpolate, log)
    for e in (e for r in results for e in r.eems):
        for name, t in _eem_tables(e, average):
            if name == "EEM":
                tables.append((e.name, t, "eem"))
            else:
                tables.append((f"{e.name} {name}", t, "extra"))
    return tables


def write_workbook(tables: list, out: Path) -> list[str]:
    used: set[str] = set()
    names = []
    with pd.ExcelWriter(out) as xw:
        for name, df, _ in tables:
            sheet = _sheet_name(name, used)
            df.to_excel(xw, sheet_name=sheet, freeze_panes=(1, 1))
            names.append(sheet)
    return names


def run(folder: Optional[Path] = None, merge: bool = True, interpolate: bool = False,
        force: Optional[str] = None, log=print, average: bool = False, recursive: bool = False,
        choose_merged_path: Optional[Callable[[Path], Optional[Path]]] = None,
        opju: bool = False, contour: bool = False, pptx: bool = False,
        files: Optional[list] = None) -> list[Path]:
    """Convert a folder, or a hand-picked list of files (files=[...]; outputs go next to them).
    merge=True: one workbook (+ .opju, + .pptx) for everything.
    merge=False: one workbook (+ .opju) per input file, in a 'cleaned' subfolder."""
    if files:
        files = [Path(f).resolve() for f in files]
        parents = {f.parent for f in files}
        folder = Path(os.path.commonpath([str(p) for p in parents])) if len(parents) > 1 else files[0].parent
        results = scan_paths(files, folder, force, log, picked=True)
        stem = files[0].stem if len(files) == 1 else folder.name
    else:
        folder = Path(folder).resolve()
        results = scan_folder(folder, force, log, recursive)
        stem = folder.name
    if not results:
        log("Nothing to convert.")
        return []
    written = []
    decks = []           # (title, tables) for the PowerPoint export
    if merge:
        default = folder / f"{stem}.xlsx"
        out = choose_merged_path(default) if choose_merged_path else default
        if out is None:
            log("Cancelled.")
            return []
        log("")
        tables = plan_tables(results, interpolate, average, log)
        sheets = write_workbook(tables, out)
        decks.append((out.stem, tables))
        log(f"Wrote {out.name}: {len(sheets)} sheet(s): {', '.join(sheets[:12])}"
            + (" ..." if len(sheets) > 12 else ""))
        written.append(out)
        if opju and save_opju(out.with_suffix(".opju"), tables, log, contour):
            log(f"Wrote {out.with_suffix('.opju').name}")
            written.append(out.with_suffix(".opju"))
    else:
        out_dir = folder / OUTPUT_DIR
        out_dir.mkdir(exist_ok=True)
        used: set[str] = set()
        log("")
        for r in results:
            stem_r = r.eems[0].name if (r.eems and not r.spectra) else r.path.stem
            if stem_r in used:
                stem_r = f"{stem_r}_{r.path.suffix[1:]}"
            used.add(stem_r)
            out = out_dir / f"{stem_r}.xlsx"
            tables = plan_tables([r], interpolate, average, log)        # one file -> one sheet
            tables = [(r.path.stem if k == "group" else n, d, k) for n, d, k in tables]
            sheets = write_workbook(tables, out)
            decks.append((out.stem, tables))
            log(f"Wrote {OUTPUT_DIR}/{out.name}  ({', '.join(sheets)})")
            written.append(out)
            if opju and save_opju(out.with_suffix(".opju"), tables, log, contour):
                log(f"Wrote {OUTPUT_DIR}/{out.with_suffix('.opju').name}")
                written.append(out.with_suffix(".opju"))
    if pptx and decks:
        from .pptx_export import export_pptx
        target = (written[0].with_suffix(".pptx") if merge
                  else folder / OUTPUT_DIR / f"{stem}.pptx")
        if export_pptx(target, decks, log, contour):
            written.append(target)
    return written


__all__ = [n for n in dir() if not n.startswith("__")]
