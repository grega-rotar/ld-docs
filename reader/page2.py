"""Page 2: the fallback amount, the handwritten currency and country, and the pensioner's signature.

Found from the page's own lines, in any language: the dotted amount cells, the currency line to their
right, the country line under them, a rule across the page, then the date line (left) and the signature
line (right). Below that is ZPIZ's printed electronic-signature table, which is never looked at.
"""
from dataclasses import dataclass, field

import cv2
import numpy as np

from .layout import Box, Comb, Line, find_combs, find_hlines


@dataclass
class Page2Layout:
    amount: Comb | None
    currency: Box | None  # where the handwriting for each field can be
    country: Box | None
    signature: Box | None
    warnings: list[str] = field(default_factory=list)


def merge_lines(lines: list[Line], gap: int = 30) -> list[Line]:
    """Handwriting crossing a line cuts it into pieces: join pieces at the same height."""
    out: list[Line] = []
    for ln in sorted(lines, key=lambda l: (l.y, l.x0)):
        same = next((o for o in out if abs(o.y - ln.y) <= 5 and ln.x0 - o.x1 <= gap and ln.x1 >= o.x0 - gap), None)
        if same:
            same.x0, same.x1 = min(same.x0, ln.x0), max(same.x1, ln.x1)
        else:
            out.append(Line(ln.x0, ln.x1, ln.y))
    return sorted(out, key=lambda l: (l.y, l.x0))


def find_layout(bw: np.ndarray) -> Page2Layout:
    h, w = bw.shape
    warnings: list[str] = []
    combs = [c for c in find_combs(bw) if len(c.cells) >= 4 and c.cy < h * 0.4]
    amount = combs[0] if combs else None
    lines = merge_lines([l for l in find_hlines(bw, 120) if l.y < h * 0.75])
    if amount is None:
        warnings.append("page 2: amount cells not found")
        return Page2Layout(None, None, None, None, warnings)
    top = amount.cells[0].y

    # currency: the line right of the amount, at its height
    cur_line = next((l for l in lines if l.x0 > amount.x1 and abs(l.y - (top + amount.cells[0].h)) < 90 and l.length > 200), None)
    # a rule across (nearly) the whole page, below the amount
    rule = next((l for l in lines if l.y > top + 150 and l.length > w * 0.6), None)
    # country: the line left, between the amount and the rule
    country_line = next(
        (l for l in lines if l.y > top + 150 and (rule is None or l.y < rule.y - 20) and l.x0 < amount.x1 and l.length > 200),
        None,
    )
    # date and signature: the first pair of lines side by side under the rule; the right one is the signature
    sig_line = None
    if rule:
        below = [l for l in lines if l.y > rule.y + 100 and l.length > 200]
        for a in below:
            pair = [b for b in below if b is not a and abs(b.y - a.y) <= 8]
            if pair:
                sig_line = max([a] + pair, key=lambda l: l.x0)
                break
    for name, ln in (("currency", cur_line), ("country", country_line), ("signature", sig_line)):
        if ln is None:
            warnings.append(f"page 2: {name} line not found")

    def above(ln: Line | None, height: int, below: int = 30) -> Box | None:
        if ln is None:
            return None
        return Box(max(ln.x0 - 30, 0), max(ln.y - height, 0), ln.length + 60, height + below)

    currency = None
    if cur_line:  # from the top of the amount cells, so the printed label above stays out
        currency = Box(cur_line.x0 - 30, top - 20, cur_line.length + 60, cur_line.y - top + 50)
    return Page2Layout(amount, currency, above(country_line, 120), above(sig_line, 100, 50), warnings)


def handwriting_ink(bw: np.ndarray, area: Box) -> float:
    """Share of the area covered by strokes, after removing the writing line and printed rules.

    Print is not in these areas (they sit between a field's label and its line), so what is left is
    handwriting, or nothing.
    """
    strokes = _strokes(area.crop(bw))
    return float((strokes > 0).mean()) if strokes.size else 0.0


def _strokes(crop: np.ndarray) -> np.ndarray:
    """Ink without the writing line, printed rules and specks."""
    crop = crop.copy()
    if crop.size == 0:
        return crop
    lines = cv2.morphologyEx(crop, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (60, 1)))
    crop[cv2.dilate(lines, np.ones((5, 1), np.uint8)) > 0] = 0
    n, labels, stats, _ = cv2.connectedComponentsWithStats(crop, connectivity=8)
    x, y, w, h, area = (stats[1:, i] for i in range(5))
    keep = np.zeros(n, bool)
    keep[1:] = (
        (area >= 25)  # specks and scanner dust
        & ~((w <= 22) & (h <= 22) & (area >= 0.55 * w * h))  # the printed full stop after a line (a filled dot)
        & ~((y == 0) & (h <= 25))  # the bottoms of the label's letters, cut by the top edge
    )
    return keep[labels].astype(np.uint8) * 255


def writing_crop(gray: np.ndarray, bw: np.ndarray, area: Box, scale: float = 1.0) -> np.ndarray:
    """The handwriting alone, for the vision model: cut tight around the strokes, the writing line
    erased, on white, optionally enlarged. Less to look at means less to misread."""
    strokes = _strokes(area.crop(bw))
    ys, xs = np.nonzero(strokes)
    if not len(xs):
        return area.crop(gray)
    y0, y1, x0, x1 = max(ys.min() - 20, 0), ys.max() + 21, max(xs.min() - 25, 0), xs.max() + 26
    g = area.crop(gray)[y0:y1, x0:x1].copy()
    g[strokes[y0:y1, x0:x1] == 0] = 255  # only the strokes stay: line, dots and dust go
    g = np.pad(g, 24, constant_values=255)
    if scale != 1.0:
        g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return g
