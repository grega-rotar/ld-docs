"""Is a checkbox marked? Measured, not guessed: how much ink is inside the frame.

On the test scans an empty box scores below 0.01 and an X or tick 0.14-0.5. Between EMPTY and MARKED
the box is unclear (a stray stroke, a faint tick) and the field goes to a person.
"""
import numpy as np

from .layout import Box

EMPTY = 0.02
MARKED = 0.08
FILLED = 0.55  # more than this is a box blacked out, often a correction


def fill(bw: np.ndarray, box: Box, inset: int = 7) -> float:
    """Share of black pixels inside the frame (the frame itself left out)."""
    inner = bw[box.y + inset : box.y + box.h - inset, box.x + inset : box.x + box.w - inset]
    return float((inner > 0).mean()) if inner.size else 0.0


def state(score: float) -> str:
    if score < EMPTY:
        return "empty"
    if score < MARKED:
        return "unclear"
    if score > FILLED:
        return "filled"
    return "marked"


def confidence(score: float) -> float:
    """How sure the empty/marked call is: 1 far from the thresholds, 0.5 right between them."""
    if score < EMPTY:
        return 0.99 if score < EMPTY / 2 else 0.95
    if score < MARKED:
        return 0.5
    if score < MARKED * 1.5:
        return 0.9
    return 0.99
