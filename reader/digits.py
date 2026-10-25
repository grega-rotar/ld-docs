"""Handwritten digits in the dotted amount cells, one cell at a time, with a small CNN (ONNX, CPU).

Each cell is first measured: no strokes inside means empty, for certain. A written cell is normalised
the way the network was trained (MNIST style: the strokes cut out, scaled to 20 px, centred by mass in
28x28) and classified; the softmax probability is the digit's confidence.
"""
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import cv2
import numpy as np

from .layout import Comb

MODEL = Path(__file__).parent / "models" / "digits.onnx"
MIN_INK = 120  # stroke pixels for a cell to count as written (a digit has 400+)


@dataclass
class Cell:
    digit: int | None  # None: empty
    prob: float  # how sure: of the digit, or of the cell being empty
    ink: int
    alternatives: list[tuple[int, float]]


def cell_strokes(bw: np.ndarray, comb: Comb) -> list[np.ndarray | None]:
    """The strokes of each cell, in order (None for a cell with none).

    Ink blobs are found once over the whole row, so a digit running out of its box stays whole and goes
    to the cell holding its centre. A blob wider than a cell is digits written in one stroke ("20",
    "00"): it is cut at the cell borders and each cell keeps its own part. Dots of the outline, dust and
    strokes along several cells (an underline) are left out.
    """
    cells = comb.cells
    cw, ch = cells[0].w, cells[0].h
    x0, y0 = max(comb.x0 - cw, 0), max(min(c.y for c in cells) - ch // 2, 0)
    region = bw[y0 : max(c.y + c.h for c in cells) + ch // 2, x0 : comb.x1 + cw]
    n, labels, stats, cents = cv2.connectedComponentsWithStats(region, connectivity=8)
    out = [np.zeros(region.shape, np.uint8) for _ in cells]
    spans = [(c.x - x0, c.x - x0 + c.w, c.y - y0, c.y - y0 + c.h) for c in cells]
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 30 or (w <= 8 and h <= 8):
            continue
        if w > cw * 1.6 and h < ch * 0.3:
            continue
        cx, cy = cents[i]
        mask = labels == i
        if w > cw * 1.25:
            for k, (a, b, top, bot) in enumerate(spans):
                part = np.zeros_like(mask)
                part[:, max(a - 4, 0) : b + 4] = mask[:, max(a - 4, 0) : b + 4]
                if part.sum() >= 60 and top <= cy <= bot:
                    out[k][part] = 255
            continue
        for k, (a, b, top, bot) in enumerate(spans):
            if a <= cx < b and top + 4 <= cy <= bot - 4:
                out[k][mask] = 255
                break
    return [o if o.any() else None for o in out]


def normalise(strokes: np.ndarray) -> np.ndarray:
    """MNIST style 28x28 float image: white strokes on black, longest side 20 px, centre of mass in the middle."""
    ys, xs = np.nonzero(strokes)
    crop = strokes[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    h, w = crop.shape
    s = 20.0 / max(h, w)
    crop = cv2.resize(crop, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    out = np.zeros((28, 28), np.float32)
    h, w = crop.shape
    y, x = (28 - h) // 2, (28 - w) // 2
    out[y : y + h, x : x + w] = crop
    m = cv2.moments(out)
    if m["m00"]:
        dx, dy = 14 - m["m10"] / m["m00"], 14 - m["m01"] / m["m00"]
        out = cv2.warpAffine(out, np.float32([[1, 0, dx], [0, 1, dy]]), (28, 28))
    return out / 255.0


@cache
def _session():
    import onnxruntime as ort

    return ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])


def classify(images: list[np.ndarray]) -> np.ndarray:
    """Softmax probabilities, one row of 10 per 28x28 image."""
    if not images:
        return np.zeros((0, 10), np.float32)
    x = np.stack(images)[:, None].astype(np.float32)
    s = _session()
    logits = s.run(None, {s.get_inputs()[0].name: x})[0]
    e = np.exp(logits - logits.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def read_comb(bw: np.ndarray, comb: Comb) -> list[list[Cell]]:
    """Every cell of every group, read."""
    cells = [(gi, c) for gi, g in enumerate(comb.groups) for c in g]
    strokes = cell_strokes(bw, comb)
    ink = [int((s > 0).sum()) if s is not None else 0 for s in strokes]
    written = [i for i, (s, k) in enumerate(zip(strokes, ink)) if s is not None and k >= MIN_INK]
    probs = classify([normalise(strokes[i]) for i in written])
    out: list[list[Cell]] = [[] for _ in comb.groups]
    p_of = dict(zip(written, probs))
    for i, (gi, _) in enumerate(cells):
        if i in p_of:
            p = p_of[i]
            order = np.argsort(p)[::-1]
            out[gi].append(Cell(int(order[0]), float(p[order[0]]), ink[i], [(int(d), float(p[d])) for d in order[1:3]]))
        else:
            # a little ink (a stray dot) is not a digit, but it is not certainly empty either
            out[gi].append(Cell(None, 0.99 if ink[i] == 0 else 0.9 if ink[i] < MIN_INK / 2 else 0.6, ink[i], []))
    return out


def value(groups: list[list[Cell]]) -> tuple[float | None, str]:
    """The number the cells spell (last group = decimals), and how it looks in the boxes ('__2.500,00').

    Groups follow the form: thousands '.' units ',' decimals. A gap inside the written digits
    (e.g. '2_5') is kept in the picture and makes the amount uncertain upstream.
    """
    picture = ".".join("".join("_" if c.digit is None else str(c.digit) for c in g) for g in groups[:-1])
    picture += "," + "".join("_" if c.digit is None else str(c.digit) for c in groups[-1]) if len(groups) > 1 else ""
    whole = "".join(str(c.digit) for g in groups[:-1] for c in g if c.digit is not None)
    dec = "".join(str(c.digit) for c in groups[-1] if c.digit is not None) if len(groups) > 1 else ""
    if not whole and not dec:
        return None, picture
    return float(f"{whole or 0}.{dec.ljust(2, '0') if dec else '0'}"), picture
