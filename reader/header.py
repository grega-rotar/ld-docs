"""The printed header of page 1: case number, documentation number, beneficiary number.

The labels are in the pensioner's language ("Ämnet nummer", "Reference Number", "Broj predmeta" …), so they
are ignored: the values are found by their shape. The case number (`10344-123/2023`) is unmistakable; the
two numbers under it, in the same column, are the documentation and beneficiary numbers.
The case number is also printed sideways in the right margin ("Št. dok.: 10344-123/2023-2 ID: … VK: …");
when both readings agree, the case number is certain.
"""
import re

import cv2
import numpy as np
import pytesseract

CASE = re.compile(r"\b(\d{5}-\d{1,7}/\d{4})\b")
DIGITS = "0123456789-/"


def _lines(img: np.ndarray, config: str) -> list[dict]:
    """OCR lines with their words and positions."""
    d = pytesseract.image_to_data(img, lang="eng", config=config, output_type=pytesseract.Output.DICT)
    lines: dict[tuple, dict] = {}
    for i, text in enumerate(d["text"]):
        if not text.strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        ln = lines.setdefault(key, {"words": [], "top": d["top"][i]})
        ln["words"].append({"text": text, "left": d["left"][i], "top": d["top"][i], "conf": float(d["conf"][i])})
    return sorted(lines.values(), key=lambda ln: ln["top"])


def read_header(gray: np.ndarray) -> dict:
    """{'case_number', 'documentation_number', 'beneficiary_number'} with a confidence each (None if not found)."""
    h, w = gray.shape
    top = gray[: int(h * 0.3)]
    lines = _lines(top, "--psm 6")
    out = {"case_number": None, "documentation_number": None, "beneficiary_number": None}
    conf = dict.fromkeys(out, 0.0)
    for ln in lines:
        for wd in ln["words"]:
            m = CASE.search(wd["text"])
            if not m:
                continue
            out["case_number"] = m.group(1)
            conf["case_number"] = 0.9 if wd["conf"] > 80 else 0.7
            doc, ben = _numbers_below(top, wd["left"], wd["top"])
            out["documentation_number"], out["beneficiary_number"] = doc, ben
            # The beneficiary number carries the documentation number (1001000011 / 0100001):
            # when they agree, both readings are right.
            if doc and ben and ben[2:9] == doc:
                conf["documentation_number"] = conf["beneficiary_number"] = 0.99
            else:
                conf["documentation_number"] = 0.8 if doc and len(doc) == 7 else 0.5 if doc else 0.0
                conf["beneficiary_number"] = 0.8 if ben and len(ben) == 10 else 0.5 if ben else 0.0
            break
        if out["case_number"]:
            break
    # Numbers re-read on their own with a digit-only whitelist: Tesseract confuses 0/O and 1/l in mixed text.
    margin = read_margin(gray)
    if margin.get("case_number") and out["case_number"]:
        if margin["case_number"] == out["case_number"]:
            conf["case_number"] = 0.99
        else:
            conf["case_number"] = min(conf["case_number"], 0.6)
    elif margin.get("case_number") and not out["case_number"]:
        out["case_number"], conf["case_number"] = margin["case_number"], 0.85
    out["margin"] = margin
    return {"values": out, "confidence": conf}


def _numbers_below(top: np.ndarray, left: int, y: int) -> tuple[str | None, str | None]:
    """The two values under the case number, in its column, each line read on its own with digits only.

    The text lines are found from the ink itself (rows with ink between blank rows), starting at the
    case number's line, so a line OCR misses can never shift the other value into its place.
    """
    x0, x1 = max(left - 15, 0), left + 420
    col = top[max(y - 8, 0) : y + 260, x0:x1]
    _, bw = cv2.threshold(col, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    rows_ink = (bw > 0).sum(axis=1) > 2
    d = np.diff(np.concatenate([[0], rows_ink.astype(np.int8), [0]]))
    bands = [(a, b) for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)) if b - a >= 12]
    values: list[str | None] = []
    for a, b in bands[1:3]:  # bands[0] is the case-number line
        line = col[max(a - 6, 0) : b + 6]
        text = pytesseract.image_to_string(
            np.pad(line, 20, constant_values=255), lang="eng", config=f"--psm 7 -c tessedit_char_whitelist={DIGITS}"
        ).strip()
        values.append(text if re.fullmatch(r"\d{5,12}", text) else None)
    values += [None] * (2 - len(values))
    return values[0], values[1]


def read_margin(gray: np.ndarray) -> dict:
    """The sideways line in the right margin: 'Št. dok.: 10344-123/2023-2 ID: 140000001 VK: 123456789'."""
    h, w = gray.shape
    strip = gray[int(h * 0.45) : int(h * 0.95), int(w * 0.9) :]
    rot = cv2.rotate(strip, cv2.ROTATE_90_CLOCKWISE)
    text = pytesseract.image_to_string(rot, lang="eng", config="--psm 6")
    out = {}
    m = re.search(r"(\d{5}-\d{1,7}/\d{4})(?:-\d+)?", text)
    if m:
        out["case_number"] = m.group(1)
    m = re.search(r"ID[:.]?\s*(\d{6,})", text)
    if m:
        out["document_id"] = m.group(1)
    m = re.search(r"VK[:.]?\s*(\d{6,})", text)
    if m:
        out["vk"] = m.group(1)
    out["text"] = text.strip()
    return out
