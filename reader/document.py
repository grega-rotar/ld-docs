"""One document (page 1 + page 2 images) → the same result the Claude version gives, read locally.

Fields, a confidence per field, `uncertain_fields` (everything below 0.9) and `notes` in Slovenian for
the clerk. Every confidence is earned: from measured ink, or from two readers agreeing (a currency must also
belong to the country). Things wrong with the form itself (a half-filled row, NO ticked but rows filled) are
notes: the reading is right, the clerk still has to know.
"""
import time

import numpy as np
from PIL import Image

from . import amount as A
from . import handwriting as HW
from . import marks as M
from . import page1 as P1
from . import page2 as P2
from .header import read_header
from .layout import deskew, ink, to_array, upright

CHECK_BELOW = 0.9
FIELDS = (
    ["case_number", "documentation_number", "beneficiary_number", "other_country"]
    + [f"{k}{i}" for i in (1, 2, 3) for k in ("amount", "currency", "country")]
    + ["amount", "currency", "country", "signature_present"]
)
SIGNATURE_INK = 0.003  # share of the signature area covered by strokes; signed pages 0.008+, unsigned < 0.0015


def _choice(bw: np.ndarray, choice: P1.Choice | None, what: str) -> tuple[str | None, float, str]:
    """The marked box of a row: (label, confidence, note)."""
    if choice is None:
        return None, 0.5, f"{what}: polj ni bilo mogoče najti"
    scores = [M.fill(bw, b) for b in choice.boxes]
    states = [M.state(s) for s in scores]
    marked = [i for i, s in enumerate(states) if s == "marked"]
    filled = [i for i, s in enumerate(states) if s == "filled"]
    unclear = [i for i, s in enumerate(states) if s == "unclear"]
    label = lambda i: choice.labels[i] if i < len(choice.labels) else None  # noqa: E731
    if len(marked) == 1:
        i = marked[0]
        conf = min(M.confidence(s) for s in scores)
        note = ""
        if label(i) is None:
            return None, 0.4, f"{what}: označeno polje brez prebrane oznake"
        if filled or unclear:
            conf = min(conf, 0.75)
            note = f"{what}: označeno {label(i)}, ob tem še " + ", ".join(
                f"{label(j) or '?'} ({'počrnjeno' if j in filled else 'nejasno'})" for j in filled + unclear
            )
        return label(i), conf, note
    if len(marked) > 1:
        return None, 0.4, f"{what}: označenih več polj ({', '.join(str(label(i)) for i in marked)})"
    if filled:
        return None, 0.5, f"{what}: polje {label(filled[0])} je počrnjeno (morda popravek)"
    if unclear:
        return None, 0.5, f"{what}: v polju {label(unclear[0])} je droben znak, morda oznaka"
    return None, min(M.confidence(s) for s in scores), ""


def read_document(pages: list[Image.Image], second_opinion: bool = True) -> dict:
    started = time.monotonic()
    out: dict = dict.fromkeys(FIELDS)
    conf: dict[str, float] = {}
    notes: list[str] = []
    warnings: list[str] = []

    g1 = deskew(_upright(pages[0], 1, warnings))
    bw1 = ink(g1)
    head = read_header(g1)
    for k in ("case_number", "documentation_number", "beneficiary_number"):
        out[k], conf[k] = head["values"][k], head["confidence"][k] if head["values"][k] else 0.3
    margin = head["values"].get("margin", {})

    lay1 = P1.find_layout(g1, bw1)
    warnings += lay1.warnings
    v, c, n = _choice(bw1, lay1.yes_no, "DA/NE")
    out["other_country"] = {"yes": True, "no": False}.get(v) if v else None
    conf["other_country"] = c
    notes += [n] if n else []
    for i in (1, 2, 3):
        block = lay1.blocks[i - 1] if i <= len(lay1.blocks) else None
        amt = A.read(g1, bw1, block.amount if block else None, second_opinion)
        out[f"amount{i}"], conf[f"amount{i}"] = amt.value, amt.confidence
        notes += [f"vrstica {i}, znesek: {amt.note}"] if amt.note else []
        for kind, what in (("currency", "valuta"), ("country", "država")):
            v, c, n = _choice(bw1, getattr(block, kind) if block else None, f"vrstica {i}, {what}")
            out[f"{kind}{i}"], conf[f"{kind}{i}"] = v, c
            notes += [n] if n else []
        _row_check(out, notes, i)

    if len(pages) > 1:
        g2 = deskew(_upright(pages[1], 2, warnings))
        bw2 = ink(g2)
        lay2 = P2.find_layout(bw2)
        warnings += lay2.warnings
        amt = A.read(g2, bw2, lay2.amount, second_opinion)
        out["amount"], conf["amount"] = amt.value, amt.confidence
        notes += [f"stran 2, znesek: {amt.note}"] if amt.note else []
        country = HW.read("country", g2, bw2, lay2.country)
        currency = HW.read("currency", g2, bw2, lay2.currency, country.code)
        for k, f in (("country", country), ("currency", currency)):
            out[k], conf[k] = f.value, f.confidence
            notes += [f"stran 2, {'država' if k == 'country' else 'valuta'}: {f.note}"] if f.note else []
        out["country_code"] = country.code  # extra key: ISO 3166 of the written country (LDDokumentP2 ignores it)
        if lay2.signature is None:
            out["signature_present"], conf["signature_present"] = None, 0.3
        else:
            sig = P2.handwriting_ink(bw2, lay2.signature)
            out["signature_present"] = sig >= SIGNATURE_INK
            conf["signature_present"] = 0.99 if sig < SIGNATURE_INK / 2 or sig > SIGNATURE_INK * 2 else 0.75
        _page2_check(out, notes)
    else:
        for k in ("amount", "currency", "country", "signature_present"):
            out[k], conf[k] = None, 0.99

    if out["other_country"] is False and any(out[f"amount{i}"] or out[f"currency{i}"] for i in (1, 2, 3)):
        notes.append("označeno NE, a vrstice z zneski so izpolnjene")
    if margin.get("case_number") and out["case_number"] and margin["case_number"] != out["case_number"]:
        notes.append(f"številka zadeve v glavi ({out['case_number']}) in na robu ({margin['case_number']}) se razlikujeta")

    out["confidence"] = {k: round(conf.get(k, 0.5), 2) for k in FIELDS}
    out["uncertain_fields"] = [k for k in FIELDS if conf.get(k, 0.5) < CHECK_BELOW]
    out["notes"] = "; ".join(notes)
    out["warnings"] = warnings
    out["margin"] = {k: margin.get(k) for k in ("case_number", "document_id", "vk")}
    out["duration_ms"] = int((time.monotonic() - started) * 1000)
    return out


def _upright(page: Image.Image, n: int, warnings: list) -> np.ndarray:
    gray, turned = upright(to_array(page), n)
    if turned:
        warnings.append(f"page {n} was scanned turned by {turned}°; read turned back")
    return gray


def _row_check(out: dict, notes: list, i: int) -> None:
    """A row is all or nothing: an amount needs a currency and a country. A half-filled row is the
    form's problem, not a misreading, so it is a note for the clerk; the fields keep their confidence."""
    have = {k: out[f"{k}{i}"] is not None for k in ("amount", "currency", "country")}
    if any(have.values()) and not all(have.values()):
        missing = [k for k, v in have.items() if not v]
        names = {"amount": "znesek", "currency": "valuta", "country": "država"}
        notes.append(f"vrstica {i}: izpolnjeno le delno (manjka {', '.join(names[k] for k in missing)})")


def _page2_check(out: dict, notes: list) -> None:
    have = {k: out[k] is not None for k in ("amount", "currency", "country")}
    if any(have.values()) and not all(have.values()):
        missing = [k for k, v in have.items() if not v]
        names = {"amount": "znesek", "currency": "valuta", "country": "država"}
        notes.append(f"stran 2: izpolnjeno le delno (manjka {', '.join(names[k] for k in missing)})")
