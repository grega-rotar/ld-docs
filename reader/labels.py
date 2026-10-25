"""The printed code next to each checkbox (EUR, HRK, D, BIH …), read with Tesseract and matched to known codes.

The codes are read, not assumed from a box's position, so a form version with another currency list
or another order still comes out right. A label that matches nothing well is returned as None, and
the field is then reported as uncertain instead of guessed.
"""
import numpy as np
import pytesseract
from rapidfuzz import fuzz, process

from .layout import Box

CURRENCIES = ["EUR", "BAM", "HRK", "RSD", "MKD", "CHF", "AUD", "USD", "GBP", "SEK", "NOK", "DKK", "CAD"]
COUNTRIES = ["D", "A", "I", "CH", "AUS", "HR", "BIH", "SRB", "MN", "MA", "MNE", "MK", "SLO", "F", "S"]
# Yes and no in the languages of Europe and of the countries Slovenian pensioners live in. Only used to
# check which box is which; if the words can't be read, the first box is yes (true on every form so far).
YES = ["JA", "DA", "YES", "OUI", "SI", "SÌ", "SÍ", "SIM", "ДА", "ТАК", "TAK", "ANO", "ÁNO", "IGEN", "JAH", "JĀ", "TAIP",
       "KYLLÄ", "PO", "EVET", "ΝΑΙ", "NAI", "JES", "IA"]  # fmt: skip
NO = ["NEJ", "NEI", "NE", "NO", "NON", "NEIN", "НЕ", "НЕТ", "НІ", "NEE", "NIE", "NEM", "NU", "EI", "NĒ", "JO", "HAYIR",
      "ΟΧΙ", "OXI", "NÃO", "NAO", "NË"]  # fmt: skip

# The orders printed so far. A row that matches one gets its labels from it (filling any letter OCR
# missed, like a lone "I"); a row that matches none keeps what OCR read, so a new form version still works.
LAYOUTS = {
    "currency": [
        ["EUR", "BAM", "HRK", "MKD", "CHF", "AUD", "RSD"],  # 2021
        ["EUR", "BAM", "RSD", "MKD", "CHF", "AUD"],  # 2023 (HRK gone with the euro in Croatia)
    ],
    "country": [["D", "A", "I", "CH", "AUS", "HR", "BIH", "SRB", "MN", "MA"]],
}
VOCAB = {"currency": CURRENCIES, "country": COUNTRIES}

# Cyrillic letters that look like Latin ones: a Cyrillic form prints the same codes, read either way.
_LOOKALIKE = str.maketrans("АВЕКМНОРСТХУІЈΑΒΕΗΙΚΜΝΟΡΤΥΧΖ", "ABEKMHOPCTXYIJABEHIKMNOPTYXZ")  # Cyrillic and Greek


def word_score(text: str, words: list[str]) -> float:
    """How much a printed word (punctuation and OCR specks stripped) looks like one of `words`, 0-100."""
    clean = "".join(c for c in text.upper() if c.isalpha()).translate(_LOOKALIKE)
    if not clean:
        return 0.0
    if clean in {w.translate(_LOOKALIKE) for w in words}:
        return 100.0  # exact beats near: "NO" is no, though it is two letters from Czech "ANO" (yes)
    return 0.9 * match(clean, words, 0)[1]


def match(text: str, vocab: list[str], cutoff: float = 70) -> tuple[str | None, float]:
    """Closest known code and the match score 0-100; (None, score) below the cutoff."""
    if not text:
        return None, 0.0
    latin = text.translate(_LOOKALIKE)
    best = process.extractOne(latin, vocab + [v.translate(_LOOKALIKE) for v in vocab], scorer=fuzz.ratio)
    if best is None:
        return None, 0.0
    code, score, idx = best
    code = vocab[idx % len(vocab)]
    return (code if score >= cutoff else None), float(score)


def read_row(gray: np.ndarray, row: list[Box], words: bool = False) -> list[str]:
    """The label of every box in a row, with one OCR call.

    The boxes (and any mark in them) are blanked out of the strip and every recognised character goes
    to the nearest box on its left. Characters, not words: Tesseract joins neighbouring labels into one word.
    Codes are printed in Latin letters on every form; only the yes/no words (words=True) are in the
    form's language, read with Tesseract's script models, which cover every language written in the
    Latin, Cyrillic or Greek alphabet.
    """
    x0, x1 = row[0].x, min(row[-1].x + row[-1].w + 160, gray.shape[1])
    y0 = max(min(b.y for b in row) - 6, 0)
    y1 = max(b.y + b.h for b in row) + 6
    strip = gray[y0:y1, x0:x1].copy()
    for b in row:
        strip[:, max(b.x - x0 - 4, 0) : b.x - x0 + b.w + 3] = 255
    pad = 20
    img = np.pad(strip, pad, constant_values=255)
    if words:
        boxes = pytesseract.image_to_boxes(img, lang="Latin+Cyrillic+Greek", config="--psm 7")
    else:
        boxes = pytesseract.image_to_boxes(img, lang="eng", config="--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    out = [""] * len(row)
    for line in boxes.splitlines():
        ch, left, _, right, _ = line.split(" ")[:5]
        cx = (int(left) + int(right)) / 2 - pad + x0
        owners = [i for i, b in enumerate(row) if b.x + b.w <= cx]
        if owners and ch.strip():
            out[owners[-1]] += ch.upper()
    return out
