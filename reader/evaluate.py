"""Read a folder of scans locally and compare every field with reference results.

    python -m reader.evaluate ld_test results/claude_ref.json [--fix fixes.json] [--out results/local.json]

The reference is a /v1/batch result (e.g. from the Claude version); --fix overrides reference values a
person checked on the scan ({"00000226.tif": {"currency": "EUR"}}). The number that matters most is
"confidently wrong": a value that differs from the reference while the reader claimed >= 0.9.
Scans hold personal data: keep the reference and output in results/ (git-ignored).
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path


from app.pages import group, load_pages, unpack

from .document import CHECK_BELOW, FIELDS, read_document
from .names import fold


def same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(float(a) - float(b)) < 0.005
    if isinstance(a, str) and isinstance(b, str):
        return fold(a) == fold(b)
    return a == b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scans")
    ap.add_argument("reference")
    ap.add_argument("--fix")
    ap.add_argument("--out")
    ap.add_argument("--only", nargs="*", help="first page names to run, e.g. 00000226.tif")
    a = ap.parse_args()

    ref = {d["pages"][0]: d for d in json.load(open(a.reference))["documents"] if d.get("status") == "done"}
    fixes = json.load(open(a.fix)) if a.fix else {}
    files = [(p.name, p.read_bytes()) for p in sorted(Path(a.scans).iterdir()) if p.is_file()]
    pages = [pg for name, data in unpack(files) for pg in load_pages(name, data)]
    docs = group(pages)

    results, wrong, confident_wrong, flagged, total = [], Counter(), [], Counter(), 0
    for doc in docs:
        first = doc.pages[0].name
        if a.only and first not in a.only:
            continue
        r = ref.get(first)
        out = read_document([p.image for p in doc.pages[:2]])
        out.update({"pages": [p.name for p in doc.pages], **(doc.qr or {})})
        results.append(out)
        if r is None:
            print(first, "no reference")
            continue
        r = {**r, **fixes.get(first, {})}
        bad = []
        for k in FIELDS:
            total += 1
            if out["confidence"][k] < CHECK_BELOW:
                flagged[k] += 1
            if not same(out[k], r.get(k)):
                wrong[k] += 1
                sure = out["confidence"][k] >= CHECK_BELOW
                bad.append(f"{k}: {out[k]!r} (ref {r.get(k)!r}, {out['confidence'][k]}{' CONFIDENTLY WRONG' if sure else ''})")
                if sure:
                    confident_wrong.append(f"{first} {k}")
        print(first, f"{out['duration_ms'] / 1000:.1f}s", "OK" if not bad else "; ".join(bad), flush=True)

    n = len(results)
    print(f"\n{n} documents, {total} fields")
    print(f"agree with reference: {total - sum(wrong.values())}/{total}")
    print(f"sent to a person (< {CHECK_BELOW}): {sum(flagged.values())} fields  {dict(flagged)}")
    print(f"CONFIDENTLY WRONG: {len(confident_wrong)}  {confident_wrong}")
    if a.out:
        Path(a.out).write_text(json.dumps({"documents": results}, ensure_ascii=False, indent=1))
    return 1 if confident_wrong else 0


if __name__ == "__main__":
    sys.exit(main())
