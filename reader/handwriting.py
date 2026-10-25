"""A handwritten page 2 field (currency or country) → a real value, or an honest "not sure".

1. Ink is measured first: no strokes → empty, certain, and the model is not asked.
2. The model reads the strokes twice (normal and enlarged crop).
3. The readings pick the closest real names from every language (names.py), and the model picks among
   them: recognising a name is far easier for it than spelling it out.
4. Confidence comes from agreement, not from the model's say-so: both readings spelling the chosen
   name → high; a correction, a disagreement or ink the model calls empty → below 0.9, for a person.
"""
from dataclasses import dataclass

import numpy as np

from . import names as N
from . import vlm
from .layout import Box
from .page2 import handwriting_ink, writing_crop

INK_EMPTY = 0.0015  # share of the field covered by strokes; written fields are 0.015+, empty ones < 0.001


@dataclass
class Field:
    value: str | None  # currency: ISO code (or as written when it is no currency); country: as written
    code: str | None  # ISO 4217 / ISO 3166 alpha-2
    confidence: float
    note: str = ""


def read(kind: str, gray: np.ndarray, bw: np.ndarray, area: Box | None, country_hint: str | None = None) -> Field:
    if area is None:
        return Field(None, None, 0.5, "polja ni bilo mogoče najti")
    ink = handwriting_ink(bw, area)
    if ink < INK_EMPTY:
        return Field(None, None, 0.99 if ink < INK_EMPTY / 3 else 0.93)
    crops = [writing_crop(gray, bw, area), writing_crop(gray, bw, area, 2.0)]
    reads = [vlm.ask(kind, c) for c in crops]
    kinds = {r["kind"] for r in reads}
    texts = [r["text"] for r in reads if r["kind"] == "text"]
    if not texts:
        k = reads[0]["kind"]
        note = {"signature": "v polju je podpis, ne vrednost", "crossed_out": "polje je prečrtano"}.get(k, "v polju so poteze, ki niso besedilo")
        sure = len(kinds) == 1 and k != "empty"
        return Field(None, None, 0.85 if sure else 0.6, note)
    if len(kinds) > 1:  # one reading saw text, the other a signature or crossing-out
        return Field(None, None, 0.4, f"nejasno, ali je v polju besedilo ({texts[0]}) ali podpis/prečrtanje")

    options = N.options(kind, texts)
    shown = [o.closest_spelling(texts) if kind == "country" else o.display for o in options]
    pick_i = vlm.choose(kind, crops[0], shown) if options else None
    if pick_i is None:
        # no real name fits: keep what was written, for a person to check
        return Field(texts[0].upper(), None, 0.5, f"prebrano »{texts[0]}«, ne ustreza nobeni znani vrednosti")
    pick = options[pick_i]
    exact = [N.exact(kind, t) for t in texts]  # readings that already are a real name
    agree = len({N.fold(t) for t in texts}) == 1
    read_as = next((e for e in exact if e), None)
    if read_as and agree and read_as.code != pick.code:
        # Both readings spell a real name, the choice says another country: trust the spelling, but ask.
        pick = read_as
        value = pick.code if kind == "currency" else pick.closest_spelling(texts)
        return _checked(kind, Field(value, pick.code, 0.8, f"prebrano »{texts[0]}«, izbira modela se ne ujema"), country_hint)
    value = pick.code if kind == "currency" else shown[pick_i]
    if any(e and e.code == pick.code for e in exact):
        same_spelling = kind == "currency" or any(N.fold(t) == N.fold(value) for t in texts)
        return _checked(kind, Field(value, pick.code, 0.97 if agree and same_spelling else 0.9), country_hint)
    return _checked(
        kind, Field(value, pick.code, 0.75, f"prebrano »{texts[0]}«, popravljeno v najbližjo znano vrednost {value}"), country_hint
    )


def _checked(kind: str, f: Field, country_hint: str | None) -> Field:
    """A currency the country never used (since 1990) is suspicious, however clearly it was read."""
    if kind == "currency" and country_hint and N.currencies_of(country_hint) and f.code not in N.currencies_of(country_hint):
        return Field(f.value, f.code, min(f.confidence, 0.6), f"valuta {f.code} ni valuta države {country_hint}")
    return f
