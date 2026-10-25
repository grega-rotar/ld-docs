"""The local reader on synthetic forms (tests/reader/synth.py), with the vision model stubbed out.

Run inside the reader image (Tesseract, OpenCV, the digit model):
    docker build -f reader/Dockerfile -t ld-reader . && docker run --rm -v $PWD:/src -w /src ld-reader python -m pytest -q tests/reader
How well it reads real scans is measured with `python -m reader.evaluate` (README "Local reader").
"""
import numpy as np
import pytest
from PIL import Image, ImageDraw

from reader import amount as A
from reader import digits as D
from reader import document, handwriting, layout, marks, names, page1, page2, vlm

from . import synth as S


def arr(im: Image.Image):
    g = layout.deskew(layout.to_array(im))
    return g, layout.ink(g)


@pytest.fixture
def no_vlm(monkeypatch):
    """The vision model answers like a careful reader of the synthetic pages."""
    answers = {"currency": "SEK", "country": "ŠVEDSKA"}
    monkeypatch.setattr(vlm, "ask", lambda field, crop, prompt=None: {"kind": "text", "text": answers[field]})
    monkeypatch.setattr(vlm, "choose", lambda field, crop, options: next((i for i, o in enumerate(options) if names.fold(o) == names.fold(answers[field])), None))
    monkeypatch.setattr(vlm, "read_amount", lambda crop: "EMPTY")
    return answers


# --- layout ---------------------------------------------------------------------------------------


def test_finds_rows_labels_and_cells_on_page1():
    g, bw = arr(S.page1(S.Page1()))
    lay = page1.find_layout(g, bw)
    assert not lay.warnings
    assert lay.yes_no.labels == ["yes", "no"]
    assert len(lay.blocks) == 3
    for b in lay.blocks:
        assert b.currency.labels == S.CUR_2021
        assert b.country.labels == S.COUNTRIES
        assert [len(gr) for gr in b.amount.groups] == [3, 3, 2]


def test_still_finds_everything_on_a_turned_shifted_scan():
    im = S.page1(S.Page1(rows=[S.Row("__1.234,56", "EUR", "D"), S.Row(), S.Row()]))
    im = im.rotate(0.8, fillcolor=255, resample=Image.BICUBIC, translate=(35, -40))
    g, bw = arr(im)
    lay = page1.find_layout(g, bw)
    assert not lay.warnings
    assert [len(b.currency.boxes) for b in lay.blocks] == [7, 7, 7]
    assert abs(layout.skew_angle(bw)) < 0.1


def test_other_resolutions_are_scaled_to_300_dpi():
    im = S.page1(S.Page1())
    small = im.resize((im.width * 2 // 3, im.height * 2 // 3))  # a 200 dpi render
    g, bw = arr(small)
    assert g.shape[0] == layout.A4_HEIGHT
    assert len(page1.find_layout(g, bw).blocks) == 3


def test_a_new_currency_list_is_read_from_the_labels_not_assumed():
    cur = ["EUR", "CHF", "USD", "GBP", "SEK"]
    g, bw = arr(S.page1(S.Page1(currencies=cur, rows=[S.Row("", "USD", "A"), S.Row(), S.Row()])))
    lay = page1.find_layout(g, bw)
    assert lay.blocks[0].currency.labels == cur
    assert any("unknown currency layout" in w for w in lay.warnings)  # reported, so someone looks at it
    assert document._choice(bw, lay.blocks[0].currency, "valuta")[0] == "USD"


def test_yes_no_words_decide_which_box_is_yes():
    g, bw = arr(S.page1(S.Page1(yes=False, yes_word="NO", no_word="YES")))  # printed the other way round
    lay = page1.find_layout(g, bw)
    assert lay.yes_no.labels == ["no", "yes"]


# --- marks ----------------------------------------------------------------------------------------


def test_marks_are_measured():
    g, bw = arr(S.page1(S.Page1(rows=[S.Row("", "HRK", "BIH"), S.Row(), S.Row()])))
    lay = page1.find_layout(g, bw)
    assert document._choice(bw, lay.blocks[0].currency, "valuta")[:2] == ("HRK", 0.99)
    assert document._choice(bw, lay.blocks[0].country, "država")[0] == "BIH"
    assert document._choice(bw, lay.blocks[1].currency, "valuta") == (None, 0.99, "")


def test_two_ticked_boxes_are_not_guessed():
    g, bw = arr(S.page1(S.Page1(rows=[S.Row("", "EUR", "D"), S.Row(), S.Row()], extra_marks=[(0, "currency", "CHF")])))
    lay = page1.find_layout(g, bw)
    value, conf, note = document._choice(bw, lay.blocks[0].currency, "valuta")
    assert value is None and conf < 0.9 and "več polj" in note


def test_mark_states():
    assert marks.state(0.0) == "empty"
    assert marks.state(0.04) == "unclear"
    assert marks.state(0.3) == "marked"
    assert marks.state(0.8) == "filled"
    assert marks.confidence(0.04) < 0.9


# --- amounts --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,value",
    [("2.500,00", 2500.0), ("2500,00", 2500.0), ("1.234,5", 1234.5), ("350,-", 350.0), ("99.00", 99.0),
     ("1.500", 1500.0), ("EMPTY", None), ("", None), ("009.546,20", 9546.2)],
)  # fmt: skip
def test_parse_amount(text, value):
    assert A.parse(text) == value


def test_amount_needs_both_readers_to_agree(monkeypatch):
    g, bw = arr(S.page1(S.Page1(rows=[S.Row("__2.500,00", "EUR", "D"), S.Row(), S.Row()])))
    comb = page1.find_layout(g, bw).blocks[0].amount
    monkeypatch.setattr(vlm, "read_amount", lambda crop: "2.500,00")
    agreed = A.read(g, bw, comb)
    assert agreed.value == 2500.0 and agreed.confidence >= 0.9
    monkeypatch.setattr(vlm, "read_amount", lambda crop: "2.900,00")
    disputed = A.read(g, bw, comb)
    assert disputed.confidence < 0.9 and "različno" in disputed.note


def test_empty_cells_are_empty_without_asking(monkeypatch):
    g, bw = arr(S.page1(S.Page1()))
    monkeypatch.setattr(vlm, "read_amount", lambda crop: pytest.fail("the model must not be asked about empty cells"))
    out = A.read(g, bw, page1.find_layout(g, bw).blocks[0].amount)
    assert out.value is None and out.confidence == 0.99


# --- names ----------------------------------------------------------------------------------------


def test_names_in_any_language():
    assert names.exact("country", "ŠVEDSKA").code == "SE"
    assert names.exact("country", "Sverige").code == "SE"
    assert names.exact("country", "Шведска").code == "SE"
    assert names.exact("currency", "švedska krona").code == "SEK"
    assert names.fold("ŠVEĐSKA") == "SVEDSKA"
    assert "LTL" in names.currencies_of("LT") and "EUR" in names.currencies_of("LT")
    assert names.exact("country", "POLSKA").closest_spelling(["POLSKA"]) == "POLSKA"
    assert any(o.code == "ES" for o in names.options("country", ["SPANIDA"]))


# --- handwriting (page 2) -------------------------------------------------------------------------


def _p2(**kw):
    g, bw = arr(S.page2(S.Page2(**kw)))
    return g, bw, page2.find_layout(bw)


def test_empty_page2_never_asks_the_model(monkeypatch):
    monkeypatch.setattr(vlm, "ask", lambda *a, **k: pytest.fail("asked about an empty field"))
    g, bw, lay = _p2()
    assert handwriting.read("country", g, bw, lay.country).value is None
    assert handwriting.read("country", g, bw, lay.country).confidence >= 0.9


def test_handwriting_agreeing_readings_are_trusted(no_vlm):
    g, bw, lay = _p2(currency="SEK", country="ŠVEDSKA")
    country = handwriting.read("country", g, bw, lay.country)
    assert (country.value, country.code) == ("ŠVEDSKA", "SE") and country.confidence >= 0.9
    currency = handwriting.read("currency", g, bw, lay.currency, country.code)
    assert (currency.value, currency.confidence) == ("SEK", 0.97)


def test_handwriting_corrected_reading_goes_to_a_person(monkeypatch, no_vlm):
    g, bw, lay = _p2(country="ŠPANIJA")
    monkeypatch.setattr(vlm, "ask", lambda field, crop, prompt=None: {"kind": "text", "text": "SPANIDA"})
    monkeypatch.setattr(vlm, "choose", lambda field, crop, options: next(i for i, o in enumerate(options) if o == "ŠPANIJA"))
    f = handwriting.read("country", g, bw, lay.country)
    assert f.value == "ŠPANIJA" and f.confidence < 0.9 and "SPANIDA" in f.note


def test_currency_must_fit_the_country(no_vlm):
    g, bw, lay = _p2(currency="SEK", country="ŠVEDSKA")
    f = handwriting.read("currency", g, bw, lay.currency, "PL")
    assert f.confidence < 0.9 and "PL" in f.note


def test_a_signature_in_a_value_field_is_not_a_value(monkeypatch):
    g, bw, lay = _p2(currency="SEK")
    monkeypatch.setattr(vlm, "ask", lambda field, crop, prompt=None: {"kind": "signature", "text": ""})
    f = handwriting.read("currency", g, bw, lay.currency)
    assert f.value is None and "podpis" in f.note


def test_signature_is_measured():
    g, bw, lay = _p2(signed=True)
    assert page2.handwriting_ink(bw, lay.signature) > document.SIGNATURE_INK
    g, bw, lay = _p2(signed=False)
    assert page2.handwriting_ink(bw, lay.signature) < document.SIGNATURE_INK / 2


# --- a whole document -----------------------------------------------------------------------------


def test_read_document(no_vlm, monkeypatch):
    monkeypatch.setattr(vlm, "read_amount", lambda crop: "1.200,50")
    p1 = S.page1(S.Page1(yes=True, rows=[S.Row(), S.Row(), S.Row()]))
    p2 = S.page2(S.Page2(amount="__1.200,50", currency="SEK", country="ŠVEDSKA", signed=True))
    out = document.read_document([p1, p2])
    assert out["case_number"] == "10344-123/2023"
    assert out["documentation_number"] == "0100001" and out["beneficiary_number"] == "1001000011"
    assert out["confidence"]["documentation_number"] == 0.99  # the beneficiary number contains it
    assert out["other_country"] is True
    assert [out[f"amount{i}"] for i in (1, 2, 3)] == [None, None, None]
    assert (out["amount"], out["currency"], out["country"], out["signature_present"]) == (1200.5, "SEK", "ŠVEDSKA", True)
    assert out["country_code"] == "SE"
    assert set(out["confidence"]) == set(document.FIELDS)
    assert out["uncertain_fields"] == [k for k in document.FIELDS if out["confidence"][k] < 0.9]


def test_a_half_filled_row_is_flagged(no_vlm, monkeypatch):
    monkeypatch.setattr(vlm, "read_amount", lambda crop: "EMPTY")
    p1 = S.page1(S.Page1(rows=[S.Row("", "EUR", "D"), S.Row(), S.Row()]))  # currency and country, no amount
    out = document.read_document([p1])
    assert out["currency1"] == "EUR" and out["confidence"]["currency1"] < 0.9
    assert "vrstica 1" in out["notes"]


@pytest.mark.parametrize("digit", range(10))
def test_digit_model_reads_each_digit(digit):
    img = Image.new("L", (90, 100), 0)
    ImageDraw.Draw(img).text((22, 8), str(digit), font=S.font(72), fill=255)
    probs = D.classify([D.normalise(np.array(img))])[0]
    assert probs.argmax() == digit
