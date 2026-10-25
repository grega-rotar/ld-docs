"""Every country and currency name in every language CLDR knows (~700), to turn a reading into a real value.

A handwritten "ŠVEDSKA", "Sverige", "Шведска" or "Sweden" all land on SE; "švedske krone", "SEK" or
"kronor" on SEK. Historic currencies (LTL, DEM, HRK …) are included, because a pension can be old.
"""
import datetime
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from functools import cache

from babel import Locale
from babel.localedata import locale_identifiers
from babel.numbers import get_territory_currencies
from rapidfuzz import fuzz

_OWN_LETTERS = str.maketrans({"Đ": "D", "Ł": "L", "Ø": "O", "Ħ": "H", "Ŧ": "T", "Æ": "AE", "Œ": "OE", "ẞ": "SS"})
# Slovenian first: the forms go to Slovenian pensioners abroad, who mostly write the Slovenian name
# (ŠVEDSKA, NEMČIJA). Only used to order equally good matches; any language can win.
PREFERRED = ("sl", "en", "de", "hr", "sr", "fr", "it", "sv", "mk", "bs")


def fold(s: str) -> str:
    """Upper case, accents and punctuation off, Cyrillic and Greek kept: the shape that is compared.
    Letters NFKD keeps whole (Đ, Ł, Ø …) are mapped by hand."""
    s = unicodedata.normalize("NFKD", s.upper().replace("ß", "SS")).translate(_OWN_LETTERS)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


@dataclass(frozen=True)
class Name:
    folded: str
    display: str  # as CLDR spells it, upper case: ŠVEDSKA
    code: str  # ISO 3166 alpha-2 or ISO 4217
    lang: str
    spellings: tuple[str, ...] = ()  # every language's spelling that folds the same (POLSKA, PÓLSKA)

    def closest_spelling(self, written: list[str]) -> str:
        """Of the real spellings, the one nearest to what was read: POLSKA, not the Icelandic PÓLSKA."""
        reads = [w.upper() for w in written if w]
        if not reads or not self.spellings:
            return self.display
        return max(self.spellings, key=lambda s: (max(fuzz.ratio(s, r) for r in reads), s == self.display))


@cache
def _index() -> tuple[list[Name], list[Name], frozenset[str]]:
    langs = [i for i in locale_identifiers() if "_" not in i]
    langs.sort(key=lambda l: (PREFERRED.index(l) if l in PREFERRED else len(PREFERRED), l))
    countries: dict[str, Name] = {}
    currencies: dict[str, Name] = {}
    spell: dict[tuple[str, str], list[str]] = defaultdict(list)
    codes: set[str] = set()
    for ident in langs:
        try:
            loc = Locale.parse(ident)
        except Exception:
            continue
        for code, name in loc.territories.items():
            if len(code) == 2 and code.isalpha():
                countries.setdefault(fold(name), Name(fold(name), name.upper(), code, ident))
                spell["country", fold(name)].append(name.upper())
        for code, name in loc.currencies.items():
            codes.add(code)
            currencies.setdefault(fold(name), Name(fold(name), name.upper(), code, ident))
    for code in codes:
        currencies.setdefault(code, Name(code, code, code, "iso"))
    countries = {k: Name(n.folded, n.display, n.code, n.lang, tuple(dict.fromkeys(spell["country", k]))) for k, n in countries.items()}
    return list(countries.values()), list(currencies.values()), frozenset(codes)


def _score(reading: str, name: Name) -> float:
    """Whole-name similarity. (Partial matching let long names like 'Tristan da Cunha' crowd out
    the right one.)"""
    a = fold(reading)
    return float(fuzz.ratio(a, name.folded)) if a else 0.0


def shortlist(kind: str, readings: list[str], k: int = 6) -> list[tuple[Name, float]]:
    """The k real names closest to the readings, summed over readings (independent misreadings rarely
    agree on a wrong name), best first; one name per code for currencies."""
    countries, currencies, _ = _index()
    names = countries if kind == "country" else currencies
    total: dict[Name, float] = defaultdict(float)
    for r in readings:
        for n in names:
            s = _score(r, n)
            if s >= 50:
                total[n] += s / len(readings)
    ranked = sorted(total.items(), key=lambda t: -t[1])
    if kind == "currency":
        seen, out = set(), []
        for n, s in ranked:
            if n.code not in seen:
                seen.add(n.code)
                out.append((n, s))
        ranked = out
    return ranked[:k]


def options(kind: str, readings: list[str], codes: int = 4) -> list[Name]:
    """What the model gets to choose from: the closest countries (or currencies), each with its spellings
    closest to the readings plus the Slovenian one, so the choice also settles the exact spelling."""
    ranked = shortlist(kind, readings, k=60)
    top: list[str] = []
    for n, _ in ranked:
        if n.code not in top:
            top.append(n.code)
        if len(top) == codes:
            break
    out: list[Name] = []
    countries, currencies, _ = _index()
    for code in top:
        spellings = [n for n, _ in ranked if n.code == code][:2]
        if kind == "country":
            sl = next((n for n in countries if n.code == code and n.lang == "sl"), None)
            spellings += [sl] if sl else []
        else:
            spellings = [next(n for n in currencies if n.code == code and n.lang == "iso")]
        for n in spellings:
            if n not in out:
                out.append(n)
    return out


def exact(kind: str, text: str) -> Name | None:
    countries, currencies, _ = _index()
    key = fold(text)
    return next((n for n in (countries if kind == "country" else currencies) if n.folded == key), None)


@cache
def currencies_of(country: str) -> frozenset[str]:
    """Every currency the country has used since 1990 (Lithuania: LTL and EUR)."""
    try:
        return frozenset(
            get_territory_currencies(country, start_date=datetime.date(1990, 1, 1), end_date=datetime.date.today(), tender=True)
        )
    except Exception:
        return frozenset()


def is_currency_code(code: str | None) -> bool:
    return bool(code) and code in _index()[2]
