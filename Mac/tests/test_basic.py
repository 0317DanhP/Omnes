"""Smoke tests on synthetic data: `python -m pytest tests` (or run this file directly)."""
import math
import random
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from datacleaner import opju_build as OB
from datacleaner import run

M64 = (1 << 64) - 1
PAY = (0, 1, 2, 3, 5, 6, 7, 8)


def _varint(b, p):
    r = s = 0
    while True:
        c = b[p]
        p += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, p


def _fpc_decode(b, p, n):
    fcm, dfcm = [0] * 4096, [0] * 4096
    fh = dh = last = 0
    out = []
    while len(out) < n:
        ctrl = b[p]
        p += 1
        for nib in (ctrl & 15, ctrl >> 4):
            if len(out) >= n:
                break
            k = PAY[nib & 7]
            resid = int.from_bytes(b[p:p + k], "little")
            p += k
            pred = ((dfcm[dh] + last) & M64) if nib & 8 else fcm[fh]
            v = (resid ^ pred) & M64
            fcm[fh] = v
            fh = ((fh << 6) ^ (v >> 48)) & 4095
            d = (v - last) & M64
            dfcm[dh] = d
            dh = ((dh << 2) ^ (d >> 40)) & 4095
            last = v
            out.append(struct.unpack("<d", struct.pack("<Q", v))[0])
    return out, p


def decode_segments(b, n):
    vals, p = [], 0
    while len(vals) < n:
        h, p = _varint(b, p)
        c = b[p]
        p += 1
        cnt = (h + 1) // 2
        if c == 0x0C:
            v, p = _fpc_decode(b, p, cnt)
            vals += v
        elif c == 0x64:
            vals += [float("nan")] * cnt
        else:
            k = (c - 8) // 9
            vals += [struct.unpack("<d", bytes(8 - k) + b[p:p + k])[0]] * cnt
            p += k
    return vals


def same(a, b):
    return len(a) == len(b) and all((x != x and y != y) or x == y for x, y in zip(a, b))


def test_segments_roundtrip():
    rnd = random.Random(0)
    for _ in range(200):
        vals = []
        while len(vals) < rnd.randint(1, 300):
            r = rnd.random()
            vals += ([0.0] * rnd.randint(1, 8) if r < .2 else [float("nan")] * rnd.randint(1, 4) if r < .3
                     else [rnd.gauss(0, 100)])
        enc = OB.encode_segments(vals)
        assert same(decode_segments(enc, len(vals)), vals)


def test_project_structure():
    x = [300 + 0.5 * i for i in range(200)]
    sheets = [OB.Sheet(f"S{k}", OB.Column("Wavelength (nm)", x),
                       [OB.Column(str(250 + 10 * j), [math.sin(i / 7 + j) for i in range(200)]) for j in range(5)])
              for k in range(3)]
    data = OB.build([OB.Book("Book1", sheets)], OB.LineGraph(), "test")
    assert data.startswith(b"CPYUA")
    assert struct.unpack_from("<Q", data, 0xC2)[0] == len(data)          # file size field
    d = struct.unpack_from("<Q", data, 0x42)[0]                           # preview directory
    assert struct.unpack_from("<Q", data, d)[0] == 2                      # book + graph previews


def test_pipeline_on_synthetic_folder():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Cary-style CSV, two samples on different wavelength grids
        a, b = np.arange(300, 199, -1.0), np.arange(350, 249.5, -0.5)
        rows = ["A,,B,", "Wavelength (nm),Abs,Wavelength (nm),Abs"]
        for i in range(max(len(a), len(b))):
            ra = f"{a[i]},{a[i] / 1000}" if i < len(a) else ","
            rb = f"{b[i]},{b[i] / 500}" if i < len(b) else ","
            rows.append(f"{ra},{rb}")
        (tmp / "cary.csv").write_text("\n".join(rows + ["", "metadata"]))
        # Duetta-style EEM .dat
        ex = [400, 350, 300]
        lines = ["Sample\t" + "\t".join(map(str, ex)) + "\t", "nm", "Normalized by 1.00"]
        lines += [f"{em}\t" + "\t".join(f"{em * k / 100:.3f}" for k in (1, 2, 3)) + "\t" for em in range(400, 450)]
        (tmp / "S1_EEM.dat").write_text("\n".join(lines))
        # two single-spectrum files of the same kind: they share one sheet
        for k in (1, 2):
            (tmp / f"plain{k}.txt").write_text("\n".join(f"{w}\t{k * w / 100}" for w in range(500, 520)))
        written = run(tmp, merge=True, opju=True, pptx=True, log=lambda m: None)
        names = {p.suffix for p in written}
        assert names == {".xlsx", ".opju", ".pptx"}
        import zipfile
        deck = zipfile.ZipFile(next(p for p in written if p.suffix == ".pptx"))
        assert any(n.startswith("ppt/embeddings/oleObject") for n in deck.namelist())
        wb = pd.read_excel(written[0], sheet_name=None, index_col=0)
        assert list(wb) == ["cary", "Other", "S1_EEM", "S1_EEM Info"]                # one sheet per file / group
        assert list(wb["cary"].columns) == ["A", "B"]
        assert list(wb["Other"].columns) == ["plain1", "plain2"]
        assert list(wb["S1_EEM"].columns) == [400, 350, 300]
        opju = next(p for p in written if p.suffix == ".opju").read_bytes()
        d = struct.unpack_from("<Q", opju, 0x42)[0]
        assert struct.unpack_from("<Q", opju, d)[0] == 4                 # 2 books + 2 line graphs
        # hand-picked files: only those are read, output named after the single file
        picked = run(None, files=[tmp / "plain1.txt"], merge=True, log=lambda m: None)
        assert picked == [tmp / "plain1.xlsx"]
        assert list(pd.read_excel(picked[0], sheet_name=None)) == ["Other"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
