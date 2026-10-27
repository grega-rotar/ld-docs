"""A clerk's review of a batch document (confirm or correct fields, confirm the document) and the batch export."""
import csv
import io
import time

from .extract import FIELDS

TYPES = {"number": (int, float), "string": str, "boolean": bool}
CONFIRMED, CORRECTED = "confirmed", "corrected"


def typed(field: str, value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) and FIELDS[field] != "boolean":
        return False  # True is an int in Python, but not an amount
    return isinstance(value, TYPES[FIELDS[field]])


def check(body) -> tuple[dict, bool | None]:
    """The PATCH body -> (fields, confirm). Raises ValueError naming what's wrong."""
    if not isinstance(body, dict) or set(body) - {"fields", "confirm"}:
        raise ValueError('body must be {"fields": {...}, "confirm": true|false}')
    fields, confirm = body.get("fields") or {}, body.get("confirm")
    if not isinstance(fields, dict):
        raise ValueError("fields must be an object")
    if confirm is not None and not isinstance(confirm, bool):
        raise ValueError("confirm must be true or false")
    for name, f in fields.items():
        if name not in FIELDS:
            raise ValueError(f"unknown field {name!r}")
        ok = isinstance(f, dict) and (
            (f.get("action") == CONFIRMED and set(f) == {"action"})
            or (f.get("action") == CORRECTED and set(f) == {"action", "value"})
        )
        if not ok:
            raise ValueError(f'{name}: use {{"action": "confirmed"}} or {{"action": "corrected", "value": ...}}')
        if f["action"] == CORRECTED and not typed(name, f["value"]):
            raise ValueError(f"{name}: value must be a {FIELDS[name]} or null")
    return fields, confirm


def equal(a, b) -> bool:
    return a is b if isinstance(a, bool) or isinstance(b, bool) else a == b  # True == 1 in Python


def apply(doc: dict, fields: dict, confirm: bool | None) -> dict:
    """Changes doc in place. `read` stays what the reader produced; "confirmed" means the read value is right."""
    now = time.time()
    review = doc.setdefault("review", {"status": "open", "confirmed_at": None, "fields": {}})
    for name, f in fields.items():
        read = review["fields"][name]["read"] if name in review["fields"] else doc.get(name)
        value = f["value"] if f["action"] == CORRECTED else read
        if isinstance(value, str):
            value = value.strip() or None
        action = CONFIRMED if equal(value, read) else CORRECTED
        review["fields"][name] = {"action": action, "read": read, "value": value, "at": now}
        doc[name] = value
    if confirm:
        review.update(status="confirmed", confirmed_at=now)
        for name in doc.get("uncertain_fields") or []:
            if name in FIELDS and name not in review["fields"]:
                review["fields"][name] = {"action": CONFIRMED, "read": doc.get(name), "value": doc.get(name), "at": now}
    elif confirm is False:
        review.update(status="open", confirmed_at=None)
    return doc


def corrections(doc: dict) -> dict:
    fields = (doc.get("review") or {}).get("fields") or {}
    return {k: {"read": f["read"], "value": f["value"]} for k, f in fields.items() if f["action"] == CORRECTED}


def exported(doc: dict) -> dict:
    return {
        "case_number": doc.get("case_number"), "document_id": doc.get("document_id"), "vk": doc.get("vk"),
        **{k: doc.get(k) for k in FIELDS if k != "case_number"},
        "pages": doc.get("pages") or [], "status": doc.get("status"),
        "reviewed": (doc.get("review") or {}).get("status") == "confirmed",
        "corrections": corrections(doc), "uncertain_fields": doc.get("uncertain_fields") or [],
        "notes": doc.get("notes"), "warnings": doc.get("warnings") or [], "error": doc.get("error"),
    }  # fmt: skip


def export_json(job: dict) -> dict:
    return {"job_id": job["job_id"], "exported_at": time.time(), "documents": [exported(d) for d in job["documents"]]}


def cell(v) -> str:
    """Slovenian Excel: decimal comma, da/ne, lists joined, null empty."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "da" if v else "ne"
    if isinstance(v, (int, float)):
        return f"{v:.2f}".replace(".", ",")  # the only numbers are amounts: always cents
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return str(v)


CSV_COLUMNS = ["case_number", "document_id", "vk", *(k for k in FIELDS if k != "case_number"),
               "reviewed", "corrected_fields", "uncertain_fields", "pages", "status", "error"]  # fmt: skip


def export_csv(job: dict) -> bytes:
    buf = io.StringIO()
    out = csv.writer(buf, delimiter=";")
    out.writerow(CSV_COLUMNS)
    for d in job["documents"]:
        row = exported(d) | {"corrected_fields": list(corrections(d))}
        out.writerow([cell(row[c]) for c in CSV_COLUMNS])
    return ("﻿" + buf.getvalue()).encode()  # the BOM makes Excel read UTF-8 (č, š, ž)
