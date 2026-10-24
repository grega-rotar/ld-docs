"""One document (page 1 + page 2) -> the JSON ZPIZ asked for, read by Claude through claude-pool.

Each document is one `claude -p` run in its own temp dir. It may only Read ./p1.png and ./p2.png
and has to answer in SCHEMA (--json-schema).
"""
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from .pages import Document

MODEL = os.environ.get("CLAUDE_MODEL", "opus")
TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
PROMPT = (Path(__file__).parent / "prompt.md").read_text()
IMAGE_MAX_SIDE = 2000
EXIT_EXHAUSTED = 75  # claude-pool: every account is at its usage limit

# Page 1 / page 2 fields, in the order and with the types of ZPIZ's LDDokumentP1 / LDDokumentP2.
P1 = {"other_country": "boolean"} | {
    f"{k}{i}": t for i in (1, 2, 3) for k, t in (("amount", "number"), ("currency", "string"), ("country", "string"))
}
P2 = {"amount": "number", "currency": "string", "country": "string", "signature_present": "boolean"}
HEADER = {"case_number": "string", "documentation_number": "string", "beneficiary_number": "string"}
FIELDS = HEADER | P1 | P2

SCHEMA = {
    "type": "object",
    "properties": {k: {"type": [t, "null"]} for k, t in FIELDS.items()}
    | {"uncertain_fields": {"type": "array", "items": {"type": "string"}}, "notes": {"type": ["string", "null"]}},
    "required": [*FIELDS, "uncertain_fields", "notes"],
    "additionalProperties": False,
}

# Batches share MAX_PARALLEL claude runs; single calls have their own slots, so they never queue behind a batch
# (Cloudflare drops a request after ~100 s of silence).
BATCH_SLOTS = asyncio.Semaphore(int(os.environ.get("MAX_PARALLEL", "4")))
SINGLE_SLOTS = asyncio.Semaphore(int(os.environ.get("MAX_PARALLEL_SINGLE", "4")))


class ExtractError(Exception):
    def __init__(self, message: str, exhausted: bool = False):
        super().__init__(message)
        self.exhausted = exhausted


def claude_bin() -> str:
    want = os.environ.get("CLAUDE_BIN", "claude-pool")
    found = shutil.which(want) or shutil.which("claude")
    if not found:
        raise ExtractError(f"no claude binary ({want})")
    return found


def clean(raw: dict) -> dict:
    out = {}
    for k, t in FIELDS.items():
        v = raw.get(k)
        if t == "number" and (isinstance(v, bool) or not isinstance(v, (int, float))):
            v = None
        elif t == "string" and v is not None:
            v = str(v).strip() or None
        elif t == "boolean" and not isinstance(v, bool):
            v = None
        out[k] = v
    for k in ("currency1", "currency2", "currency3", "country1", "country2", "country3", "currency"):
        if out[k]:
            out[k] = out[k].upper()
    return out


async def run_claude(workdir: str, files: list[str]) -> tuple[dict, str]:
    args = [
        claude_bin(), "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
        "--model", MODEL, "--no-session-persistence", "--strict-mcp-config", "--max-turns", "6",
        "--tools", "Read", "--allowedTools", ",".join(f"Read(./{f})" for f in files),
        "--permission-mode", "dontAsk",
    ]  # fmt: skip
    env = {**os.environ, "CLAUDE_POOL_FRESH": "0"}
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=workdir, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(PROMPT.encode()), TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ExtractError(f"claude did not answer within {TIMEOUT} s") from None
    if proc.returncode == EXIT_EXHAUSTED:
        raise ExtractError("every claude-pool account is at its usage limit, try again later", exhausted=True)
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        raise ExtractError(f"claude exit {proc.returncode}: {(stderr or stdout).decode()[-300:]}") from None
    if out.get("is_error") or proc.returncode or not isinstance(out.get("structured_output"), dict):
        why = out.get("result") or out.get("errors") or stderr.decode()[-300:]
        raise ExtractError(f"claude error ({out.get('subtype')}, exit {proc.returncode}): {str(why)[:300]}")
    model = next(iter(out.get("modelUsage") or {}), MODEL)
    return out["structured_output"], model


def write_pngs(doc: Document, tmp: str) -> list[str]:
    files = []
    for i, page in enumerate(doc.pages[:2], 1):
        im = page.image.convert("L")
        im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        im.save(Path(tmp) / f"p{i}.png", optimize=True)
        files.append(f"p{i}.png")
    return files


async def extract(doc: Document, batch: bool = False) -> dict:
    """The flat result: link to the case first, then ZPIZ's 14 fields, then what to double-check."""
    async with BATCH_SLOTS if batch else SINGLE_SLOTS:
        with tempfile.TemporaryDirectory(prefix="ld-") as tmp:
            files = await asyncio.to_thread(write_pngs, doc, tmp)
            try:
                raw, model = await run_claude(tmp, files)
            except ExtractError as e:
                if e.exhausted:
                    raise
                raw, model = await run_claude(tmp, files)  # one retry: runs that start together sometimes fail
    data = clean(raw)
    qr = doc.qr or {}
    warnings = []
    if not qr:
        warnings.append("no QR code found on page 1: document_id and vk are missing")
    if len(doc.pages) < 2:
        warnings.append("page 2 is missing: amount, currency, country and signature_present are null")
    if len(doc.pages) > 2:
        warnings.append(f"{len(doc.pages)} pages, only the first two were read")
    return {
        "case_number": data.pop("case_number"),
        "document_id": qr.get("document_id"),
        "vk": qr.get("vk"),
        **data,
        "uncertain_fields": [f for f in raw.get("uncertain_fields") or [] if f in FIELDS],
        "notes": raw.get("notes"),
        "warnings": warnings,
        "pages": [p.name for p in doc.pages],
        "model": model,
    }
