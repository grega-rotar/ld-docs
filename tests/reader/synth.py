"""Synthetic LD forms for the reader's tests: the real scans hold personal data and never enter the repo.

The drawing follows the real form's geometry at 300 dpi (A4 = 2480 x 3508): checkboxes ~48 px, dotted
digit cells 90 x 100 px in groups 3-3-2, the same rows in the same order. Printed text uses DejaVu Sans;
"handwriting" is a script-like font or plain strokes, which is enough to exercise the logic.
"""
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CUR_2021 = ["EUR", "BAM", "HRK", "MKD", "CHF", "AUD", "RSD"]
COUNTRIES = ["D", "A", "I", "CH", "AUS", "HR", "BIH", "SRB", "MN", "MA"]


def font(size: int, bold: bool = False):
    return ImageFont.truetype(BOLD if bold else FONT, size)


@dataclass
class Row:
    amount: str = ""  # as written in the cells, e.g. "__2.500,00" ('_' = empty cell)
    currency: str | None = None
    country: str | None = None


@dataclass
class Page1:
    case: str = "10344-123/2023"
    documentation: str = "0100001"
    beneficiary: str = "1001000011"
    yes: bool | None = True
    yes_word: str = "JA"
    no_word: str = "NEJ"
    currencies: list[str] = field(default_factory=lambda: list(CUR_2021))
    rows: list[Row] = field(default_factory=lambda: [Row(), Row(), Row()])
    extra_marks: list[tuple[int, str, str]] = field(default_factory=list)  # (row, "currency"|"country", code)


def box(d: ImageDraw.ImageDraw, x: int, y: int, s: int = 48, mark: bool = False):
    d.rectangle([x, y, x + s, y + s], outline=0, width=4)
    if mark:
        d.line([x + 6, y + 6, x + s - 6, y + s - 6], fill=0, width=6)
        d.line([x + s - 6, y + 6, x + 6, y + s - 6], fill=0, width=6)


def dotted_rect(d: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int, step: int = 12):
    for x in range(x0, x1 + 1, step):
        d.rectangle([x, y0, x + 2, y0 + 2], fill=0)
        d.rectangle([x, y1, x + 2, y1 + 2], fill=0)
    for y in range(y0, y1 + 1, step):
        d.rectangle([x0, y, x0 + 2, y + 2], fill=0)
        d.rectangle([x1, y, x1 + 2, y + 2], fill=0)


def comb(d: ImageDraw.ImageDraw, x: int, y: int, written: str = "", cw: int = 90, ch: int = 100):
    """Dotted cells 3 . 3 , 2 at (x, y); `written` like '__2.500,00' puts digits in the cells."""
    digits = written.replace(".", "").replace(",", "").ljust(8, "_") if written else "_" * 8
    hand = font(70)
    k = 0
    for gi, n in enumerate((3, 3, 2)):
        for i in range(n):
            cx = x + i * cw
            dotted_rect(d, cx, y, cx + cw, y + ch)
            if digits[k] != "_":
                d.text((cx + 22, y + 12), digits[k], font=hand, fill=0)
            k += 1
        x += n * cw + 40
        if gi == 0:
            d.ellipse([x - 28, y + ch - 14, x - 14, y + ch], fill=0)  # the printed '.'
        elif gi == 1:
            d.text((x - 32, y + ch - 30), ",", font=font(60, True), fill=0)


def page1(spec: Page1) -> Image.Image:
    im = Image.new("L", (2480, 3508), 255)
    d = ImageDraw.Draw(im)
    f = font(36)
    for y in range(70, 290, 22):  # a QR-like block top left (the real one is read by app/pages.py, not here)
        for x in range(57, 280, 22):
            if (x * 7 + y * 13) % 5 < 3:
                d.rectangle([x, y, x + 21, y + 21], fill=0)
    for i, (label, value) in enumerate((("Ämnet nummer:", spec.case), ("Dokumentation nummer:", spec.documentation),
                                        ("Förmånstagaren nummer:", spec.beneficiary))):  # fmt: skip
        d.text((310, 395 + i * 58), label, font=f, fill=0)
        d.text((820, 395 + i * 58), value, font=f, fill=0)
    d.text((620, 580), "UTTALANDE OM BELOPPET AV UTLÄNDSK PENSION", font=font(44, True), fill=0)
    d.line([300, 1200, 2200, 1200], fill=0, width=3)
    # yes / no
    box(d, 300, 1446, mark=spec.yes is True)
    d.text((360, 1446), spec.yes_word, font=f, fill=0)
    box(d, 520, 1446, mark=spec.yes is False)
    d.text((580, 1446), spec.no_word, font=f, fill=0)
    for r, row in enumerate(spec.rows):
        y = 1860 + r * 520
        d.text((310, y - 120), "Beloppet:", font=font(34, True), fill=0)
        comb(d, 330, y - 30, row.amount)
        x = 1200
        for code in spec.currencies:
            marked = code == row.currency or (r, "currency", code) in spec.extra_marks
            box(d, x, y, mark=marked)
            d.text((x + 56, y + 2), code, font=f, fill=0)
            x += 154 if len(spec.currencies) <= 6 else 142
        x = 330
        for code in COUNTRIES:
            marked = code == row.country or (r, "country", code) in spec.extra_marks
            box(d, x, y + 236, mark=marked)
            d.text((x + 56, y + 238), code, font=f, fill=0)
            x += 160
        d.line([300, y + 350, 2200, y + 350], fill=0, width=3)
    # the sideways line in the right margin
    margin = Image.new("L", (1200, 50), 255)
    ImageDraw.Draw(margin).text((0, 0), f"Št. dok.: {spec.case}-2 ID: 140000001 VK: 123456789", font=font(32), fill=0)
    im.paste(margin.rotate(90, expand=True), (2330, 2000))
    return im


@dataclass
class Page2:
    amount: str = ""
    currency: str = ""
    country: str = ""
    signed: bool = False


def page2(spec: Page2) -> Image.Image:
    im = Image.new("L", (2480, 3508), 255)
    d = ImageDraw.Draw(im)
    f = font(36)
    hand = font(64)
    d.text((310, 130), "Nedan fältet fylls i bara i fallet att ...", font=font(38, True), fill=0)
    d.text((310, 360), "Utbetalt pension i januari 2023 är:", font=f, fill=0)
    d.text((1500, 360), "i valutan:", font=f, fill=0)
    comb(d, 330, 450, spec.amount)
    d.line([1500, 524, 2130, 524], fill=0, width=4)
    if spec.currency:
        d.text((1560, 440), spec.currency, font=hand, fill=0)
    d.text((310, 660), "från landet:", font=f, fill=0)
    d.line([320, 806, 940, 806], fill=0, width=4)
    if spec.country:
        d.text((360, 715), spec.country, font=hand, fill=0)
    d.line([316, 884, 2200, 884], fill=0, width=4)
    d.text((310, 1000), "Jag förklarar att de uppgifter som lämnats är korrekta.", font=f, fill=0)
    d.text((310, 1330), "Ort och datum:", font=f, fill=0)
    d.text((1020, 1330), "Underskrift av pensionären:", font=f, fill=0)
    d.line([320, 1470, 920, 1470], fill=0, width=4)
    d.line([1020, 1470, 2100, 1470], fill=0, width=4)
    if spec.signed:
        d.line([1200, 1460, 1300, 1380, 1350, 1455, 1450, 1390, 1520, 1450], fill=0, width=6, joint="curve")
    d.rectangle([310, 2440, 2200, 2530], outline=0, width=3)
    d.text((330, 2460), "ELEKTRONSKI PODPISI: is-zpiz-ep-test", font=font(30), fill=0)
    return im
