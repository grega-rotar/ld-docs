"""Where things are on a scanned page: checkboxes, dotted digit cells and long writing lines.

Nothing here knows coordinates of a particular form version. Everything is found by its shape,
so a form in another language, a newer year or a slightly shifted scan still works.
All sizes are in pixels at 300 dpi; pages at another resolution are scaled to 300 dpi first.
"""
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

DPI = 300
BOX_MIN, BOX_MAX = 36, 66  # a checkbox is ~4 mm square
CELL_MIN_W, CELL_MAX_W = 55, 130  # a dotted digit cell
CELL_MIN_H, CELL_MAX_H = 75, 160


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def crop(self, a: np.ndarray, pad: int = 0) -> np.ndarray:
        return a[max(self.y - pad, 0) : self.y + self.h + pad, max(self.x - pad, 0) : self.x + self.w + pad]


@dataclass
class Line:
    x0: int
    x1: int
    y: int

    @property
    def length(self) -> int:
        return self.x1 - self.x0


A4_HEIGHT = 3508  # px at 300 dpi


def to_array(image: Image.Image) -> np.ndarray:
    """Greyscale uint8 at 300 dpi.

    The scale comes from the page itself (an A4 page is 3508 px high at 300 dpi), not from the file's
    dpi tag, which PDF renders, faxes and re-saved PNGs often get wrong or leave out.
    """
    g = image.convert("L")
    scale = A4_HEIGHT / g.height
    if abs(scale - 1) > 0.03:
        g = g.resize((round(g.width * scale), A4_HEIGHT), Image.LANCZOS if scale < 1 else Image.BICUBIC)
    return np.asarray(g)


def ink(gray: np.ndarray) -> np.ndarray:
    """Black pixels as 255, white as 0. Otsu, so grey or faint scans binarise like fax ones."""
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return bw


def skew_angle(bw: np.ndarray) -> float:
    """Degrees the page is turned, from the slope of its long printed rules.

    A short horizontal opening keeps lines tilted up to ~2 degrees; each long one is fitted with a
    straight line and the median slope wins, so a few handwritten strokes cannot move it.
    """
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1)))
    h = cv2.morphologyEx(h, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(h, connectivity=8)
    angles, weights = [], []
    for i in range(1, n):
        x, y, w, hh, area = stats[i]
        if w < 500 or hh > w * 0.05:
            continue
        ys, xs = np.nonzero(labels[y : y + hh, x : x + w] == i)
        slope = np.polyfit(xs, ys, 1)[0]
        angles.append(np.degrees(np.arctan(slope)))
        weights.append(w)
    if not angles:
        return 0.0
    order = np.argsort(angles)
    cum = np.cumsum(np.array(weights)[order])
    return float(np.array(angles)[order][np.searchsorted(cum, cum[-1] / 2)])  # weighted median


def deskew(gray: np.ndarray) -> np.ndarray:
    angle = skew_angle(ink(gray))
    if abs(angle) < 0.05:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)


def _straight_lines(bw: np.ndarray, length: int) -> np.ndarray:
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1)))
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, length)))
    return h | v


def _square_holes(lines: np.ndarray, lo: int, hi: int) -> list[Box]:
    """Closed outlines of size lo..hi. Taking the hole (inner contour) ignores marks drawn across the frame."""
    cnts, hier = cv2.findContours(lines, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for i, c in enumerate(cnts):
        if hier[0][i][3] == -1:  # outer contours: we want holes
            continue
        x, y, w, h = cv2.boundingRect(c)
        if lo <= w <= hi and lo <= h <= hi and abs(w - h) <= max(6, w // 6):
            out.append(Box(x - 2, y - 2, w + 4, h + 4))
    return out


def find_checkboxes(bw: np.ndarray) -> list[Box]:
    """Printed square checkboxes (frame included), ticked or not.

    Only straight strokes of at least 25 px survive, so a tick or cross drawn over a box does not hide it.
    A frame with a gap (bad scan) is closed with a 3 px dilation.
    """
    lines = cv2.dilate(_straight_lines(bw, 25), np.ones((3, 3), np.uint8))
    boxes = _square_holes(lines, BOX_MIN - 8, BOX_MAX - 4)
    return _dedupe(boxes)


def _dots(bw: np.ndarray) -> np.ndarray:
    """Only the tiny blobs: the dots of dotted outlines (handwriting and print are bigger)."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    small = (stats[:, cv2.CC_STAT_AREA] <= 40) & (stats[:, cv2.CC_STAT_WIDTH] <= 8) & (stats[:, cv2.CC_STAT_HEIGHT] <= 8)
    small[0] = False
    return small[labels].astype(np.uint8) * 255


def _runs(mask: np.ndarray, min_len: int = 1) -> list[tuple[int, int]]:
    """(start, end) of each run of True at least min_len long."""
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return [(a, b) for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)) if b - a >= min_len]


@dataclass
class Comb:
    """A row of dotted digit cells, split into groups at the printed separators ('.' and ',')."""
    groups: list[list[Box]]

    @property
    def cells(self) -> list[Box]:
        return [c for g in self.groups for c in g]

    @property
    def cy(self) -> float:
        return self.cells[0].cy

    @property
    def x0(self) -> int:
        return self.cells[0].x

    @property
    def x1(self) -> int:
        return self.cells[-1].x + self.cells[-1].w


def find_combs(bw: np.ndarray) -> list[Comb]:
    """Rows of dotted digit cells, top to bottom.

    The outlines are found from their dots alone. A group of cells (between the printed '.' and ',')
    is where both its dotted top and bottom edges run; handwriting can hide a stretch of one edge,
    rarely the same stretch of both. The cells in a group are then counted from the cell pitch.
    """
    dots = _dots(bw)
    hj = cv2.morphologyEx(dots, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (17, 3)))
    hj = cv2.morphologyEx(hj, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (60, 1)))
    segments = [s for s in _component_lines(hj) if s.length >= 60]
    edges = _edge_lines(segments)
    combs = []
    used: set[int] = set()
    for i, (ty, top) in enumerate(edges):
        if i in used:
            continue
        for j in range(i + 1, len(edges)):
            by, bot = edges[j]
            if by - ty < CELL_MIN_H:
                continue
            if by - ty > CELL_MAX_H:
                break
            comb = _comb_between(dots, top, bot, ty, by)
            if comb:
                combs.append(comb)
                used.add(j)
                break
    return combs


def _edge_lines(segments: list[Line]) -> list[tuple[int, list[tuple[int, int]]]]:
    """Dotted edge segments at the same height, as (y, [(x0, x1), ...]) top to bottom."""
    out: list[tuple[int, list[tuple[int, int]]]] = []
    for s in sorted(segments, key=lambda s: s.y):
        if out and abs(out[-1][0] - s.y) <= 8:
            out[-1][1].append((s.x0, s.x1))
        else:
            out.append((s.y, [(s.x0, s.x1)]))
    return [(y, _union(spans, 6)) for y, spans in out]


def _union(spans: list[tuple[int, int]], join: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(spans):
        if out and a - out[-1][1] <= join:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _comb_between(dots: np.ndarray, top: list, bot: list, y0: int, y1: int) -> Comb | None:
    # Groups: where the top or the bottom edge runs, within the stretch both edges cover.
    lo = max(min(a for a, _ in top), min(a for a, _ in bot))
    hi = min(max(b for _, b in top), max(b for _, b in bot))
    if hi - lo < CELL_MIN_W:
        return None
    spans = [(max(a, lo), min(b, hi)) for a, b in _union(top + bot, 6) if min(b, hi) - max(a, lo) >= CELL_MIN_W * 0.6]
    if not spans:
        return None
    pitch = _pitch(dots, spans, y0, y1)
    if pitch is None:
        return None
    groups = []
    for a, b in spans:
        n = max(1, round((b - a) / pitch))
        if abs((b - a) - n * pitch) > 0.25 * pitch:
            return None  # not a whole number of cells: not a comb
        w = (b - a) / n
        groups.append([Box(int(a + i * w), y0, int(w), y1 - y0) for i in range(n)])
    return Comb(groups)


def _pitch(dots: np.ndarray, spans: list[tuple[int, int]], y0: int, y1: int) -> float | None:
    """Cell width, from the dotted vertical borders inside the band (two dot columns ~4 px apart)."""
    band = dots[y0 + 15 : y1 - 15] > 0
    expected = band.shape[0] / 12 * 2  # a dot every ~12 px, two columns
    gaps = []
    for a, b in spans:
        profile = np.convolve(band[:, max(a - 12, 0) : b + 12].sum(axis=0).astype(float), np.ones(11), "same")
        xs = [(p + q) / 2 for p, q in _runs(profile > 0.35 * expected)]
        gaps += [g for g in np.diff(xs) if CELL_MIN_W <= g <= CELL_MAX_W]
    if gaps:
        return float(np.median(gaps))
    return (y1 - y0) * 0.8 if CELL_MIN_W <= (y1 - y0) * 0.8 <= CELL_MAX_W else None



def _component_lines(mask: np.ndarray) -> list[Line]:
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return sorted(
        (Line(int(s[0]), int(s[0] + s[2]), int(s[1] + s[3] // 2)) for s in stats[1:] if s[3] <= 14),
        key=lambda l: (l.y, l.x0),
    )


def find_hlines(bw: np.ndarray, min_length: int = 300) -> list[Line]:
    """Solid horizontal lines at least min_length long (writing lines, rules), top to bottom."""
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (min_length, 1)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(h, connectivity=8)
    out = [
        Line(int(s[0]), int(s[0] + s[2]), int(s[1] + s[3] // 2))
        for s in stats[1:]
        if s[3] <= 12  # a line, not a filled block
    ]
    return sorted(out, key=lambda l: (l.y, l.x0))


def rows(items: list[Box], tol: float = 20) -> list[list[Box]]:
    """Boxes grouped into rows by their centre height, each row left to right, rows top to bottom."""
    out: list[list[Box]] = []
    for b in sorted(items, key=lambda b: b.cy):
        if out and abs(np.mean([o.cy for o in out[-1]]) - b.cy) <= tol:
            out[-1].append(b)
        else:
            out.append([b])
    return [sorted(r, key=lambda b: b.cx) for r in out]


def _dedupe(boxes: list[Box]) -> list[Box]:
    out: list[Box] = []
    for b in sorted(boxes, key=lambda b: -b.w * b.h):
        if all(abs(b.cx - o.cx) > o.w / 2 or abs(b.cy - o.cy) > o.h / 2 for o in out):
            out.append(b)
    return out
