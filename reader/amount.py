"""An amount in dotted digit cells: read by two independent readers, trusted when they agree.

The CNN reads every cell on its own (digits.py); the vision model reads the whole row at once. They
fail differently: the CNN on an unusual 7 or 1, the model on touching or overflowing digits. When both
give the same number, it is right; when they don't, the number goes to a person with both readings.
"""
import re
from dataclasses import dataclass

import numpy as np

from . import digits as D
from . import vlm
from .layout import Comb

CELL_SURE = 0.94  # every digit this sure (CNN softmax) for an agreed amount to skip the person; README "Accuracy"


@dataclass
class Amount:
    value: float | None
    confidence: float
    note: str = ""
    picture: str = ""  # the cells as read, '__2.500,00'


def _dash(bw: np.ndarray, comb: Comb) -> bool:
    """'350,-': a flat stroke across the decimal cells instead of digits."""
    cells = comb.groups[-1]
    x0, x1 = cells[0].x, cells[-1].x + cells[-1].w
    y0, y1 = cells[0].y, cells[0].y + cells[0].h
    region = bw[y0:y1, x0:x1] > 0
    if not region.any():
        return False
    ys, xs = np.nonzero(region[8:-8, 8:-8]) if region.shape[0] > 16 and region.shape[1] > 16 else ([], [])
    if not len(xs):
        return False
    return (ys.max() - ys.min()) < cells[0].h * 0.25 and (xs.max() - xs.min()) > cells[0].w * 0.5


def parse(text: str) -> float | None:
    """'2.500,00' / '2500,00' / '2 500' / '350,-' -> float, the comma being the decimal mark (Slovenian forms)."""
    t = text.strip().upper()
    if not t or "EMPTY" in t:
        return None
    t = re.sub(r"[^0-9.,\-]", "", t)
    if "," in t:
        whole, dec = t.rsplit(",", 1)
    elif re.search(r"\.\d{2}$", t) and t.count(".") == 1 and len(t) > 3 and not re.search(r"\.\d{3}$", t):
        whole, dec = t.rsplit(".", 1)  # '99.00' written with a dot
    else:
        whole, dec = t, ""
    whole = re.sub(r"\D", "", whole)
    dec = re.sub(r"\D", "", dec)[:2]
    if not whole and not dec:
        return None
    return float(f"{int(whole or 0)}.{dec.ljust(2, '0') if dec else '00'}")


def read(gray: np.ndarray, bw: np.ndarray, comb: Comb | None, second_opinion: bool = True) -> Amount:
    if comb is None:
        return Amount(None, 0.5, "polj za znesek ni bilo mogoče najti")
    groups = D.read_comb(bw, comb)
    cells = [c for g in groups for c in g]
    written = [c for c in cells if c.digit is not None]
    stray = [c for c in cells if c.digit is None and c.ink > 0]
    dash = len(groups) > 1 and not any(c.digit is not None for c in groups[-1]) and _dash(bw, comb)
    value, picture = D.value(groups)
    if not written:
        if stray:
            return Amount(None, 0.85, "v poljih za znesek je nekaj črnila, a ne števka", picture)
        return Amount(None, 0.99, "", picture)

    cnn_conf = min(c.prob for c in written)
    problems = []
    whole = "".join("_" if c.digit is None else "d" for g in groups[:-1] for c in g)
    if re.search(r"d_+d", whole):
        problems.append("med števkami je prazno polje")
    if dash:
        problems.append("decimalke so pomišljaj (,-)")
    if stray:
        problems.append("v praznem polju je nekaj črnila")

    if not second_opinion:
        return Amount(value, min(cnn_conf, 0.85 if problems else 0.95), "; ".join(problems), picture)

    crop = gray[max(comb.cells[0].y - 25, 0) : comb.cells[0].y + comb.cells[0].h + 35, max(comb.x0 - 15, 0) : comb.x1 + 15]
    text = vlm.read_amount(crop)
    # Compared digit by digit, not as numbers: which cells hold a digit is measured (ink), so where the
    # dot goes is known; the model often misplaces it ('2.000,00' for '200,00') but reads the digits.
    ours = "".join(str(c.digit) for c in written)
    theirs = "".join(ch for ch in text if ch.isdigit())
    if theirs == ours:
        conf = 0.97 if cnn_conf >= CELL_SURE else 0.88
        if problems:
            conf = min(conf, 0.88)
        return Amount(value, conf, "; ".join(problems), picture)
    # They differ. Where the model's digits are a second choice of the CNN in every cell that differs,
    # the model is probably right; otherwise keep the CNN's (the better reader per cell). Either way a person checks.
    if len(theirs) == len(ours) and all(
        a == b or int(b) in [d for d, p in c.alternatives if p > 0.05] for a, b, c in zip(ours, theirs, written)
    ):
        for c, b in zip(written, theirs):
            c.digit = int(b)
        alt, alt_picture = D.value(groups)
        return Amount(alt, 0.75, f"števke prebrane različno ({picture} / {text}); izbrano {alt_picture}", alt_picture)
    note = f"števke prebrane različno ({picture} / {text or 'nič'})"
    return Amount(value, min(0.6, cnn_conf), "; ".join([note] + problems), picture)
