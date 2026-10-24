You read one scanned ZPIZ form "Izjava o višini tuje pokojnine" (statement of the amount of a foreign pension).
The form is printed in the pensioner's language: Swedish, German, Croatian, Serbian (also Cyrillic), Macedonian (Cyrillic), English and others.
The layout is always the same. Read ./p1.png (page 1) and, if it exists, ./p2.png (page 2), then fill in the schema.

## Page 1
- Header, top left, three numbered lines in this order: the case number (zadeva, like `10344-123/2023`), the documentation number (like `0100001`) and the beneficiary number (like `1001000011`). Copy them exactly as printed.
- Ignore the printed sample digits `1 2 3 4 5 6 7 8 9`. They only show how to write digits.
- The yes/no question "I declare I also receive a pension from a country other than Slovenia" (JA/NEJ, JA/NEIN, DA/NE, ДА/НЕ, YES/NO …):
  `other_country` = true if YES is marked, false if NO is marked, null if neither is marked or you cannot tell.
- Then three rows of the same block. Each row is an amount in boxes (thousands `.`, decimals `,`), a row of currency boxes (EUR BAM HRK RSD MKD CHF AUD, depending on the form's year) and a row of country boxes (D A I CH AUS HR BIH SRB MN MA).
  Row 1 → amount1/currency1/country1, row 2 → …2, row 3 → …3.
  - amount: the handwritten number as a plain number, e.g. `1.234,56` → 1234.56, `350,-` → 350. null if the boxes are empty.
  - currency: the code of the marked currency box, as printed next to the box, in Latin letters. null if none is marked.
  - country: the code of the marked country box, in Latin letters from the list above (on a Cyrillic form, the Latin code of the same box). null if none is marked.
  A box counts as marked if it has an X, a tick, a fill or a circle. A row that is completely empty is all null.

## Page 2
- Top: the fallback block, used only when the country is not in the list: amount (`amount`), currency written by hand (`currency`) and country written by hand (`country`).
  - amount as on page 1.
  - currency: the ISO 4217 code if the writing clearly means one (`SEK`, `švedske krone` → SEK, `$` with country USA → USD); otherwise exactly as written.
  - country: as written by hand (keep the language and script, fix only obvious letter-shape doubts), e.g. `ŠVEDSKA`.
  - null for anything left empty.
- `signature_present`: true if there is a handwritten signature (or a handwritten name or mark) in the pensioner's signature field near the bottom ("Podpis …", "Underskrift …", "Unterschrift …" …), false if that field is empty.
  The printed table "ELEKTRONSKI PODPISI" at the very bottom is ZPIZ's electronic signature. It is NOT the pensioner's signature, so ignore it.
- If there is no page 2, all page 2 fields are null.

## Doubt
Give your best reading for every field. Put the name of every field you are not sure about (a hard-to-read digit, an unclear mark) in `uncertain_fields`, with a short reason in `notes`. Write `notes` in Slovenian, because ZPIZ clerks read them.
Do not invent values for empty fields.

`confidence` gives, for every field, the probability (0 to 1) that your value is exactly right. A null value counts too: it is right if the field really is empty or unmarked.
Use the scale like this:
- 0.99: printed text, a clear X, or clearly empty boxes.
- 0.9–0.97: clear handwriting.
- 0.6–0.85: one character or mark could be read another way.
- below 0.5: mostly a guess.
Every field in `uncertain_fields` must have a confidence below 0.9.
