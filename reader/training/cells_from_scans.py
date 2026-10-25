"""Cut labelled digit cells out of real scans, for finetune_digits.py.

    python -m reader.training.cells_from_scans ld_test results/ref.json results/cells.npz

The labels come from a reference /v1/batch result for the same scans (the Claude version, or values a
person checked). An amount is used only when the written cells line up with its digits one to one;
anything else is skipped and listed. The output holds pieces of real handwriting: keep it in results/.
"""
import argparse
import json
from pathlib import Path

import numpy as np

from app.pages import group, load_pages, unpack

from .. import digits as D
from ..layout import deskew, ink, to_array
from ..page1 import find_layout as page1_layout
from ..page2 import find_layout as page2_layout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scans")
    ap.add_argument("reference")
    ap.add_argument("out")
    a = ap.parse_args()
    ref = {d["pages"][0]: d for d in json.load(open(a.reference))["documents"] if d.get("status") == "done"}
    files = [(p.name, p.read_bytes()) for p in sorted(Path(a.scans).iterdir()) if p.is_file()]
    docs = group([pg for name, data in unpack(files) for pg in load_pages(name, data)])
    X, Y, meta = [], [], []
    for doc in docs:
        r = ref.get(doc.pages[0].name)
        if not r:
            continue
        fields = []
        g = deskew(to_array(doc.pages[0].image))
        bw = ink(g)
        for i, b in enumerate(page1_layout(g, bw).blocks, 1):
            fields.append((r.get(f"amount{i}"), b.amount, bw, f"{doc.pages[0].name}:amount{i}"))
        if len(doc.pages) > 1:
            g2 = deskew(to_array(doc.pages[1].image))
            bw2 = ink(g2)
            fields.append((r.get("amount"), page2_layout(bw2).amount, bw2, f"{doc.pages[1].name}:amount"))
        for value, comb, b, name in fields:
            if value is None or comb is None:
                continue
            strokes = D.cell_strokes(b, comb)
            written = [i for i, s in enumerate(strokes) if s is not None and (s > 0).sum() >= D.MIN_INK]
            n_dec = len(comb.groups[-1])
            whole, dec = f"{value:.2f}".split(".")
            w_cells = [i for i in written if i < len(strokes) - n_dec]
            d_cells = [i for i in written if i >= len(strokes) - n_dec]
            whole = whole.lstrip("0")
            if len(w_cells) < len(whole) or len(d_cells) != n_dec:
                print("skip", name, value, f"({len(w_cells)}+{len(d_cells)} written cells)")
                continue
            labels = ["0"] * (len(w_cells) - len(whole)) + list(whole) + list(dec)  # leading zeros may be written
            for i, ch in zip(w_cells + d_cells, labels):
                X.append(D.normalise(strokes[i]))
                Y.append(int(ch))
                meta.append(name)
    np.savez(a.out, X=np.array(X), Y=np.array(Y), meta=np.array(meta))
    print(len(Y), "cells", np.bincount(Y, minlength=10))


if __name__ == "__main__":
    main()
