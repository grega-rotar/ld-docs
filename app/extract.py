"""One document (page 1 + page 2) -> the JSON ZPIZ asked for.

Read either by the local reader (READER_URL set: reader/ on a GPU machine, see README "Local reader")
or by Claude through claude-pool: then each document is one `claude -p` run in its own temp dir that
may only Read ./p1.png and ./p2.png and has to answer in SCHEMA (--json-schema).
READERS=local (ZPIZ's own server) turns Claude off completely.
"""
import asyncio
import io
import json
import os
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path

import httpx

from .pages import Document

MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
READER_URL = os.environ.get("READER_URL", "").rstrip("/")
READER_KEY = os.environ.get("READER_KEY", "")
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
    | {
        # How sure the model is of each value (including "this field is empty"), 0-1.
        "confidence": {
            "type": "object",
            "properties": {k: {"type": "number", "minimum": 0, "maximum": 1} for k in FIELDS},
            "required": [*FIELDS],
            "additionalProperties": False,
        },
        "uncertain_fields": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": ["string", "null"]},
    },
    "required": [*FIELDS, "confidence", "uncertain_fields", "notes"],
    "additionalProperties": False,
}

# Each reader has its own slots, so comparing them never slows either down. Batches share PARALLEL[reader] runs;
# single calls have their own slots, so they never queue behind a batch (Cloudflare drops a request after ~100 s of silence).
LOCAL, CLAUDE, BOTH = "local", "claude", "both"


def enabled_readers(spec: str, reader_url: str) -> list[str]:
    """READERS (comma list) -> the readers this server offers. Unset: local too when READER_URL is set."""
    if not spec.strip():
        return [LOCAL, CLAUDE] if reader_url else [CLAUDE]
    readers = list(dict.fromkeys(r.strip() for r in spec.split(",") if r.strip()))
    if bad := [r for r in readers if r not in (LOCAL, CLAUDE)]:
        raise RuntimeError(f"READERS: unknown reader {bad}, use local and/or claude")
    if LOCAL in readers and not reader_url:
        raise RuntimeError("READERS has local but READER_URL is not set")
    return readers


# Raises at import: a misconfigured server refuses to start instead of failing every document.
READERS = enabled_readers(os.environ.get("READERS", ""), READER_URL)
DEFAULT_READER = READERS[0]
PARALLEL = {
    LOCAL: int(os.environ.get("LOCAL_PARALLEL", "3")),  # the local reader reads 2 at a time
    CLAUDE: int(os.environ.get("MAX_PARALLEL", "6")),
}
SLOTS = {
    r: {True: asyncio.Semaphore(n), False: asyncio.Semaphore(int(os.environ.get("MAX_PARALLEL_SINGLE", "4")))}
    for r, n in PARALLEL.items()
}


HEALTH_TTL = 15
_reader_health: dict[str, tuple[float, dict]] = {}  # READER_URL -> (checked at, answer)


def reader_health() -> dict:
    """{"ok", "model"} from the local reader's /health, cached so GET /health stays fast. Never raises."""
    hit = _reader_health.get(READER_URL)
    if hit and time.monotonic() - hit[0] < HEALTH_TTL:
        return hit[1]
    try:
        r = httpx.get(f"{READER_URL}/health", timeout=3)
        body = r.json() if r.status_code == 200 else {}
        out = {"ok": body.get("model_loaded") is True, "model": body.get("model")}
    except Exception:  # unreachable, too slow or not JSON: either way the reader can't read now
        out = {"ok": False, "model": None}
    _reader_health[READER_URL] = (time.monotonic(), out)
    return out


def parallel_of(reader: str) -> int:
    return min(PARALLEL[r] for r in (READERS if reader == BOTH else [reader]))


class ExtractError(Exception):
    def __init__(self, message: str, exhausted: bool = False):
        super().__init__(message)
        self.exhausted = exhausted


def claude_bin() -> str:
    if CLAUDE not in READERS:  # READERS=local (ZPIZ): scans must never reach Claude, whatever asks
        raise ExtractError("claude is not enabled on this server (READERS)")
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


def usage_of(out: dict) -> dict:
    """Tokens and the list-price cost of one run, summed over models (claude-pool bills a subscription, not this)."""
    models = (out.get("modelUsage") or {}).values()
    total = lambda k: sum(int(m.get(k) or 0) for m in models)  # noqa: E731
    return {
        "input_tokens": total("inputTokens"),
        "output_tokens": total("outputTokens"),
        "cache_read_tokens": total("cacheReadInputTokens"),
        "cache_write_tokens": total("cacheCreationInputTokens"),
        "cost_usd": round(float(out.get("total_cost_usd") or 0), 5),
    }


def no_progress(stage: str, **info):
    pass


async def run_claude(workdir: str, files: list[str], progress=no_progress) -> tuple[dict, str, dict]:
    """One streamed claude run. progress(stage, **info) hears what it really does:
    starting -> page1 -> page2 -> thinking -> writing (fields=n, of=total)."""
    # Our prompt replaces Claude Code's own system prompt, and no settings, skills or plugins load:
    # that cuts the fixed overhead of a run from ~32k to ~1.5k tokens.
    args = [
        claude_bin(), "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--json-schema", json.dumps(SCHEMA),
        "--model", MODEL, "--no-session-persistence", "--strict-mcp-config", "--max-turns", "6",
        "--system-prompt", PROMPT, "--disable-slash-commands", "--setting-sources", "",
        "--tools", "Read", "--allowedTools", ",".join(f"Read(./{f})" for f in files),
        "--permission-mode", "dontAsk",
    ]  # fmt: skip
    task = f"Read {' and '.join(f'./{f}' for f in files)}, then answer in the schema."
    env = {**os.environ, "CLAUDE_POOL_FRESH": "0"}
    progress("starting")
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=workdir, env=env, limit=64 * 1024 * 1024,  # tool results carry the page images as base64
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    proc.stdin.write(task.encode())
    proc.stdin.close()

    async def follow() -> dict | None:
        result, answer = None, None
        async for line in proc.stdout:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = e.get("type")
            if kind == "assistant":
                for c in e.get("message", {}).get("content", []):
                    page = str((c.get("input") or {}).get("file_path", ""))
                    if c.get("type") == "tool_use" and c.get("name") == "Read" and page.endswith(tuple(files)):
                        progress(f"page{page[-5]}")  # ./p1.png -> page1
            elif kind == "stream_event":
                ev = e.get("event", {})
                block = ev.get("content_block") or {}
                delta = ev.get("delta") or {}
                if block.get("type") == "thinking":
                    progress("thinking")
                elif block.get("type") == "tool_use" and block.get("name") == "StructuredOutput":
                    answer = ""
                    progress("writing", fields=0, of=len(FIELDS))
                elif delta.get("type") == "input_json_delta" and answer is not None:
                    before = answer.count('":')
                    answer += delta.get("partial_json", "")
                    if answer.count('":') != before:
                        done = sum(f'"{k}":' in answer for k in FIELDS)
                        progress("writing", fields=done, of=len(FIELDS))
            elif kind == "result":
                result = e
        return result

    try:
        out = await asyncio.wait_for(follow(), TIMEOUT)
        await asyncio.wait_for(proc.wait(), 10)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ExtractError(f"claude did not answer within {TIMEOUT} s") from None
    stderr = (await proc.stderr.read()).decode(errors="replace")
    if proc.returncode == EXIT_EXHAUSTED:
        raise ExtractError("every claude-pool account is at its usage limit, try again later", exhausted=True)
    if not out:
        raise ExtractError(f"claude exit {proc.returncode}: {stderr[-300:]}")
    if out.get("is_error") or proc.returncode or not isinstance(out.get("structured_output"), dict):
        why = out.get("result") or out.get("errors") or stderr[-300:]
        raise ExtractError(f"claude error ({out.get('subtype')}, exit {proc.returncode}): {str(why)[:300]}")
    model = next(iter(out.get("modelUsage") or {}), MODEL)
    return out["structured_output"], model, usage_of(out)


def page_png(page) -> bytes:
    """A page for the local reader: full resolution, lossless (1-bit scans stay 1-bit, so ~100 KB)."""
    buf = io.BytesIO()
    im = page.image if page.image.mode in ("1", "L") else page.image.convert("L")
    im.save(buf, format="PNG", optimize=False, compress_level=3)
    return buf.getvalue()


async def run_local(doc: Document, progress=no_progress) -> dict:
    """One document through the local reader (reader/ on the GPU machine, behind a Cloudflare Tunnel)."""
    progress("preparing")
    files = [("files", (f"p{i}.png", await asyncio.to_thread(page_png, p), "image/png")) for i, p in enumerate(doc.pages[:2], 1)]
    progress("reading")
    last = None
    for attempt in range(2):  # one retry: the tunnel or the laptop can blink
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=15)) as c:
                r = await c.post(f"{READER_URL}/v1/read", files=files, headers={"Authorization": f"Bearer {READER_KEY}"})
            if r.status_code == 200:
                return r.json()
            last = f"reader answered {r.status_code}: {r.text[:300]}"
            if r.status_code < 500:
                break
        except httpx.HTTPError as e:
            last = f"reader unreachable ({type(e).__name__}: {e})"
        if attempt == 0:
            progress("retrying")
            await asyncio.sleep(2)
    raise ExtractError(last or "reader failed", exhausted=last is not None and "unreachable" in last)


def write_pngs(doc: Document, tmp: str) -> list[str]:
    files = []
    for i, page in enumerate(doc.pages[:2], 1):
        im = page.image.convert("L")
        im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        im.save(Path(tmp) / f"p{i}.png", optimize=True)
        files.append(f"p{i}.png")
    return files


def doc_warnings(doc: Document) -> list[str]:
    warnings = []
    if not doc.qr:
        warnings.append("no QR code found on page 1: document_id and vk are missing")
    if len(doc.pages) < 2:
        warnings.append("page 2 is missing: amount, currency, country and signature_present are null")
    if len(doc.pages) > 2:
        warnings.append(f"{len(doc.pages)} pages, only the first two were read")
    return warnings


async def extract_local(doc: Document, progress=no_progress) -> dict:
    started = time.monotonic()
    raw = await run_local(doc, progress)
    qr = doc.qr or {}
    warnings = doc_warnings(doc) + list(raw.get("warnings") or [])
    margin = raw.get("margin") or {}
    for k in ("document_id", "vk"):  # the QR code is exact; the margin text is OCR, so it only warns
        if qr.get(k) and margin.get(k) and margin[k] != qr[k]:
            warnings.append(f"{k} in the QR code ({qr[k]}) differs from the margin text ({margin[k]})")
    conf = raw.get("confidence") or {}
    qr_conf = 1.0 if qr else None
    data = {k: raw.get(k) for k in FIELDS}
    return {
        "case_number": data.pop("case_number"),
        "document_id": qr.get("document_id"),
        "vk": qr.get("vk"),
        **data,
        "country_code": raw.get("country_code"),
        "confidence": {"case_number": conf.get("case_number"), "document_id": qr_conf, "vk": qr_conf}
        | {k: conf.get(k) for k in FIELDS if k != "case_number"},
        "uncertain_fields": [f for f in raw.get("uncertain_fields") or [] if f in FIELDS],
        "notes": raw.get("notes") or None,
        "warnings": warnings,
        "pages": [p.name for p in doc.pages],
        "model": raw.get("model") or "local",
        "usage": {
            "duration_ms": round((time.monotonic() - started) * 1000),
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0,
        },  # fmt: skip
    }


async def extract(doc: Document, batch: bool = False, progress=no_progress, reader: str | None = None) -> dict:
    """The flat result: link to the case first, then ZPIZ's 14 fields, then what to double-check.
    reader "both" reads with the local reader and Claude at once and adds `compare` (see compare())."""
    reader = reader or DEFAULT_READER
    if reader == BOTH:
        return await compare(doc, batch, progress)
    async with SLOTS[reader][batch]:
        return await READ[reader](doc, progress)


async def extract_claude(doc: Document, progress=no_progress) -> dict:
    started = time.monotonic()  # after the queue: this is how long reading took, not waiting
    progress("preparing")
    with tempfile.TemporaryDirectory(prefix="ld-") as tmp:
        files = await asyncio.to_thread(write_pngs, doc, tmp)
        try:
            raw, model, usage = await run_claude(tmp, files, progress)
        except ExtractError as e:
            if e.exhausted:
                raise
            progress("retrying")
            raw, model, usage = await run_claude(tmp, files, progress)  # runs that start together sometimes fail
    usage = {"duration_ms": round((time.monotonic() - started) * 1000), **usage}
    data = clean(raw)
    qr = doc.qr or {}
    warnings = doc_warnings(doc)
    conf = raw.get("confidence") or {}
    conf = {k: round(min(max(float(conf[k]), 0.0), 1.0), 2) if isinstance(conf.get(k), (int, float)) else None for k in FIELDS}
    qr_conf = 1.0 if qr else None  # read from the QR code, not guessed
    return {
        "case_number": data.pop("case_number"),
        "document_id": qr.get("document_id"),
        "vk": qr.get("vk"),
        **data,
        "confidence": {"case_number": conf.pop("case_number"), "document_id": qr_conf, "vk": qr_conf, **conf},
        "uncertain_fields": [f for f in raw.get("uncertain_fields") or [] if f in FIELDS],
        "notes": raw.get("notes"),
        "warnings": warnings,
        "pages": [p.name for p in doc.pages],
        "model": model,
        "usage": usage,
    }


READ = {LOCAL: extract_local, CLAUDE: extract_claude}


def fold(v: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", v) if not unicodedata.combining(c)).casefold().strip()


def same(a, b) -> bool:
    """Equal as ZPIZ would see it: amounts to the cent, names without case or accents (Litva = LITVA)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b  # True == 1 in Python
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return round(a, 2) == round(b, 2)
    if isinstance(a, str) and isinstance(b, str):
        return fold(a) == fold(b)
    return a == b


async def compare(doc: Document, batch: bool, progress=no_progress) -> dict:
    """Both readers on the same document. The local result is the answer (Claude's if the local one failed);
    `compare` holds the other reader's whole result and the fields where the two differ, or its error."""
    async def read(reader, report):
        async with SLOTS[reader][batch]:
            return await READ[reader](doc, report)

    # Claude reports the finer stages, so the progress shown is its own.
    local, claude = await asyncio.gather(read(LOCAL, no_progress), read(CLAUDE, progress), return_exceptions=True)
    if isinstance(local, BaseException) and isinstance(claude, BaseException):
        raise local
    main, other, name = (claude, local, LOCAL) if isinstance(local, BaseException) else (local, claude, CLAUDE)
    if isinstance(other, BaseException):
        main["compare"] = {"reader": name, "error": str(other) or type(other).__name__}
    else:
        keys = ["case_number", "document_id", "vk", *(k for k in FIELDS if k != "case_number")]
        main["compare"] = {"reader": name, "differences": [k for k in keys if not same(main.get(k), other.get(k))], "result": other}
    return main
