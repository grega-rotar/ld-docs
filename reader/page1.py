"""Page 1: find its parts (yes/no question, three blocks of amount + currency + country) on any layout.

Nothing is placed by fixed coordinates. Rows of checkboxes are told apart by the codes printed next to
them, and the three blocks repeat the same rows, so a box or digit cell hidden under a heavy mark in one
block is put back from the other two.
"""
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from . import labels as LB
from .layout import Box, Comb, find_checkboxes, find_combs, rows


@dataclass
class Choice:
    """A row of checkboxes and what each one means."""
    boxes: list[Box]
    labels: list[str | None]


@dataclass
class Block:
    amount: Comb | None
    currency: Choice | None
    country: Choice | None


@dataclass
class Page1Layout:
    yes_no: Choice | None
    blocks: list[Block]
    warnings: list[str] = field(default_factory=list)


def find_layout(gray: np.ndarray, bw: np.ndarray) -> Page1Layout:
    warnings: list[str] = []
    box_rows = [r for r in rows(find_checkboxes(bw)) if len(r) >= 2]
    read = [(r, LB.read_row(gray, r)) for r in box_rows]

    typed: dict[str, list[list[Box]]] = {"currency": [], "country": []}
    labels_read: dict[str, list[list[str]]] = {"currency": [], "country": []}
    for r, texts in read:
        kind = _kind(texts)
        if kind:
            typed[kind].append(r)
            labels_read[kind].append(texts)

    choices: dict[str, list[Choice]] = {}
    for kind in ("currency", "country"):
        choices[kind], warn = _complete(typed[kind], labels_read[kind], kind)
        warnings += warn
    if len(choices["currency"]) != 3 or len(choices["country"]) != 3:
        warnings.append(f"page 1: found {len(choices['currency'])} currency and {len(choices['country'])} country rows, expected 3 each")

    first_choice = min([c.boxes[0].cy for k in choices.values() for c in k], default=gray.shape[0])
    yes_no = _yes_no(gray, [r for r in box_rows if len(r) == 2 and r[0].cy < first_choice])
    if yes_no is None:
        warnings.append("page 1: yes/no boxes not found")

    combs = _complete_combs([c for c in find_combs(bw) if len(c.cells) >= 2], [c.boxes[0].cy for c in choices["currency"]])
    blocks = []
    for i, cur in enumerate(choices["currency"]):
        nxt = choices["currency"][i + 1].boxes[0].cy if i + 1 < len(choices["currency"]) else 1e9
        country = next((c for c in choices["country"] if cur.boxes[0].cy < c.boxes[0].cy < nxt), None)
        blocks.append(Block(combs[i], cur, country))
    return Page1Layout(yes_no, blocks, warnings)


def _kind(texts: list[str]) -> str | None:
    """'currency' or 'country' when most labels in the row are such codes."""
    for kind in ("currency", "country"):
        hits = sum(1 for t in texts if LB.match(t, LB.VOCAB[kind], 80)[0])
        if hits >= max(2, len(texts) * 0.6):
            return kind
    return None


def _columns(rows_: list[list[Box]], tol: float = 25) -> list[float]:
    xs = sorted(b.cx for r in rows_ for b in r)
    cols: list[list[float]] = []
    for x in xs:
        if cols and x - np.mean(cols[-1]) <= tol:
            cols[-1].append(x)
        else:
            cols.append([x])
    return [float(np.median(c)) for c in cols]


def _complete(rows_: list[list[Box]], texts: list[list[str]], kind: str) -> tuple[list[Choice], list[str]]:
    """Every row of one kind gets a box in every column, and every column a label by vote of all rows."""
    if not rows_:
        return [], []
    warnings = []
    cols = _columns(rows_)
    w = int(np.median([b.w for r in rows_ for b in r]))
    h = int(np.median([b.h for r in rows_ for b in r]))
    votes: list[Counter] = [Counter() for _ in cols]
    filled_rows = []
    for r, t in zip(rows_, texts):
        cy = float(np.median([b.cy for b in r]))
        boxes = []
        for ci, x in enumerate(cols):
            near = [(b, t[i]) for i, b in enumerate(r) if abs(b.cx - x) <= 25]
            if near:
                b, text = near[0]
                code, _ = LB.match(text, LB.VOCAB[kind], 70)
                if code:
                    votes[ci][code] += 1
                boxes.append(b)
            else:  # hidden under a mark or a bad scan: the column says where it is
                boxes.append(Box(int(x - w / 2), int(cy - h / 2), w, h))
        filled_rows.append(boxes)
    read = [v.most_common(1)[0][0] if v else None for v in votes]
    layout = _known_layout(read, kind)
    if layout:
        final = layout
    else:
        final = read
        warnings.append(f"page 1: unknown {kind} layout {read}")
    return [Choice(b, list(final)) for b in filled_rows], warnings


def _known_layout(read: list[str | None], kind: str) -> list[str] | None:
    for layout in LB.LAYOUTS[kind]:
        if len(layout) != len(read):
            continue
        known = [(a, b) for a, b in zip(read, layout) if a]
        if known and sum(a == b for a, b in known) >= 0.8 * len(known) and len(known) >= len(layout) // 2:
            return layout
    return None


def _yes_no(gray: np.ndarray, candidates: list[list[Box]]) -> Choice | None:
    """The two-box row nearest above the first currency row. Its words decide which box is yes;
    if they can't be read, the first box is yes (true on every form so far)."""
    if not candidates:
        return None
    row = candidates[-1]
    texts = LB.read_row(gray, row, words=True)
    lean = [LB.word_score(t, LB.YES) - LB.word_score(t, LB.NO) for t in texts]  # > 0: looks like yes
    # Swap only when both words clearly say so ("NO … YES"); unread or ambiguous words keep the usual order.
    labels = ["no", "yes"] if lean[0] < -20 and lean[1] > 20 else ["yes", "no"]
    return Choice(row, labels)


def _complete_combs(combs: list[Comb], row_ys: list[float]) -> list[Comb | None]:
    """The amount comb of each currency row. A comb broken by handwriting is replaced by the cells of the
    most common clean comb on the page, moved to that row."""
    if not row_ys:
        return []
    by_row: list[Comb | None] = []
    for y in row_ys:
        near = [c for c in combs if abs(c.cy - y) < 70]
        by_row.append(max(near, key=lambda c: len(c.cells)) if near else None)
    shapes = Counter(tuple(len(g) for g in c.groups) for c in by_row if c)
    if not shapes:
        return by_row
    shape = shapes.most_common(1)[0][0]
    template = next(c for c in by_row if c and tuple(len(g) for g in c.groups) == shape)
    offsets = [c.cy - y for c, y in zip(by_row, row_ys) if c and tuple(len(g) for g in c.groups) == shape]
    dy = float(np.median(offsets))
    out = []
    for c, y in zip(by_row, row_ys):
        if c and tuple(len(g) for g in c.groups) == shape:
            out.append(c)
            continue
        top = int(y + dy - template.cells[0].h / 2) if c is None else c.cells[0].y
        out.append(Comb([[Box(b.x, top, b.w, b.h) for b in g] for g in template.groups]))
    return out
