"""HTTP API: POST /v1/extract (one document, waits for the answer) and /v1/batch (many, polled by job id)."""
import asyncio
import json
import os
import secrets
import time
import uuid
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import extract as ex
from . import review
from .extract import ExtractError, extract
from .pages import group, load_pages, unpack

API_KEYS = [k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()]
JOBS_DIR = Path(os.environ.get("JOBS_DIR", "/data/jobs"))
JOB_TTL = int(os.environ.get("JOB_TTL_DAYS", "7")) * 86400  # results hold personal data: delete them after this

app = FastAPI(title="LD documents", version="1", docs_url="/docs", redoc_url=None)
bearer = HTTPBearer(auto_error=False)
# Jobs still working live here, so polling sees every stage without touching the disk.
# A job on disk that says "running" but isn't here was cut off by a restart.
_live: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()


def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not API_KEYS:
        raise HTTPException(503, "no API_KEYS configured on the server")
    if not cred or not any(secrets.compare_digest(cred.credentials, k) for k in API_KEYS):
        raise HTTPException(401, "missing or wrong API key (Authorization: Bearer <key>)")


def split(files: list[tuple[str, bytes]], progress=lambda done, total: None):
    """Files -> documents. Raises ValueError for anything that isn't a readable scan."""
    try:
        pages = [p for name, data in unpack(files) for p in load_pages(name, data)]
    except OSError as e:
        raise ValueError(str(e)) from None
    if not pages:
        raise ValueError("no pages in the upload")
    return group(pages, progress)


async def read_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    return [(f.filename or "upload", await f.read()) for f in files]


async def documents(files: list[UploadFile]):
    try:
        return await asyncio.to_thread(split, await read_uploads(files))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/", include_in_schema=False)
def ui():
    """Upload page; it calls the API below with the key the user types in."""
    return FileResponse(Path(__file__).parent / "ui.html")


def readers() -> list[str]:
    return ex.READERS + [ex.BOTH] * (len(ex.READERS) > 1)


def pick(reader: str | None) -> str:
    reader = reader or ex.DEFAULT_READER
    if reader not in readers():
        raise HTTPException(400, f"reader must be one of {readers()}")
    return reader


# The form field `reader` on /v1/extract and /v1/batch.
READER = Form(None, description='"local", "claude", or "both" (reads with both and adds `compare`); default: GET /health "reader"')


@app.get("/health")
def health():
    out = {"ok": True, "reader": ex.DEFAULT_READER, "readers": readers(), "job_ttl_days": JOB_TTL // 86400}
    if ex.LOCAL in ex.READERS:
        out["local"] = ex.reader_health()
    return out


@app.post("/v1/extract", dependencies=[Depends(auth)])
async def extract_one(files: list[UploadFile] = File(...), reader: str | None = READER):
    """One document: page 1 and page 2 as two files, or one multi-page TIFF/PDF."""
    reader = pick(reader)
    docs = await documents(files)
    if len(docs) > 1:
        raise HTTPException(400, f"found {len(docs)} documents (pages {[d.pages[0].name for d in docs]}); "
                                 "send one document here, or use /v1/batch")
    try:
        return await extract(docs[0], reader=reader)
    except ExtractError as e:
        raise HTTPException(503 if e.exhausted else 502, str(e)) from None


def job_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def save(job: dict):
    tmp = job_path(job["job_id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False))
    tmp.replace(job_path(job["job_id"]))


async def run_job(job: dict, files: list[tuple[str, bytes]]):
    try:
        docs = await asyncio.to_thread(split, files, lambda done, total: job.update(split={"done": done, "total": total}))
    except ValueError as e:
        job.update(status="error", error=str(e), finished_at=time.time())
        return
    finally:
        files.clear()  # the uploaded bytes are no longer needed
    job.update(status="running", total=len(docs), documents=[
        {"status": "pending", "stage": "queued", "pages": [p.name for p in d.pages]} for d in docs])
    save(job)

    def progress_of(i):
        def progress(stage, **info):
            d = job["documents"][i]
            if stage == "preparing":
                d["started_at"] = time.time()
            d.update(stage=stage, **info)
        return progress

    async def one(i, doc):
        try:
            job["documents"][i] = {"status": "done", **await extract(doc, batch=True, progress=progress_of(i), reader=job["reader"])}
        except Exception as e:
            job["documents"][i] = {"status": "error", "error": str(e), "pages": [p.name for p in doc.pages]}
        job["done"] += 1
        save(job)

    await asyncio.gather(*(one(i, d) for i, d in enumerate(docs)))
    job["status"] = "done"
    job["finished_at"] = time.time()


async def follow_job(job: dict, files):
    try:
        await run_job(job, files)
    finally:
        save(job)
        _live.pop(job["job_id"], None)


def cleanup():
    for f in JOBS_DIR.glob("*.json"):
        if time.time() - f.stat().st_mtime > JOB_TTL:
            f.unlink(missing_ok=True)


@app.post("/v1/batch", dependencies=[Depends(auth)], status_code=202)
async def batch(files: list[UploadFile] = File(...), reader: str | None = READER):
    """Many documents: loose pages (sorted by name, e.g. 00000001.tif, 00000002.tif …), multi-page files or a ZIP.
    Answers right away; the pages are split into documents (at the QR code on each page 1) in the background.
    Poll GET /v1/batch/{job_id}: status goes splitting -> running -> done (or error)."""
    reader = pick(reader)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup()
    job = {
        "job_id": uuid.uuid4().hex, "status": "splitting", "created_at": time.time(),
        "reader": reader, "parallel": ex.parallel_of(reader),
        "split": {"done": 0, "total": 0}, "total": None, "done": 0, "documents": [],
    }  # fmt: skip
    _live[job["job_id"]] = job
    save(job)
    task = asyncio.create_task(follow_job(job, await read_uploads(files)))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {k: job[k] for k in ("job_id", "status", "total")}


def load_job(job_id: str) -> dict:
    """The live job (the very dict the batch updates) while it runs, else the one on disk."""
    if job_id in _live:
        return _live[job_id]
    if not job_id.isalnum() or not job_path(job_id).exists():  # isalnum: job_id becomes a file name
        raise HTTPException(404, "no such job (results are deleted after JOB_TTL_DAYS)")
    return json.loads(job_path(job_id).read_text())


@app.get("/v1/batch/{job_id}", dependencies=[Depends(auth)])
def batch_status(job_id: str):
    """While a job runs, each pending document has `stage`: queued, preparing, starting, page1, page2, thinking,
    writing (with `fields` / `of`) or retrying, and `started_at` once it has a slot."""
    job = load_job(job_id)
    if job_id not in _live and job["status"] in ("splitting", "running"):
        job["status"] = "interrupted"  # the server restarted while it ran; send the files again
    return JSONResponse(job)


# async: on the event loop, so a review and the running batch never save() the same job at once.
@app.patch("/v1/batch/{job_id}/documents/{index}", dependencies=[Depends(auth)])
async def review_document(job_id: str, index: int, body: dict = Body(...)):
    """A clerk's review: {"fields": {"amount": {"action": "corrected", "value": 2500.02},
    "currency": {"action": "confirmed"}}, "confirm": true}. Returns the document with its `review`."""
    job = load_job(job_id)
    if not 0 <= index < len(job["documents"]):
        raise HTTPException(404, f"no document {index} in this job")
    doc = job["documents"][index]
    if doc.get("status") != "done":
        raise HTTPException(409, f"document {index} is {doc.get('status')}: only read documents can be reviewed")
    try:
        fields, confirm = review.check(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    review.apply(doc, fields, confirm)
    save(job)  # a job cut off by a restart keeps its status on disk; only GET calls it interrupted
    return doc


@app.get("/v1/batch/{job_id}/export", dependencies=[Depends(auth)])
async def export(job_id: str, format: str = "json"):
    """The batch with the clerks' corrections: json (ZPIZ's fields + review) or csv (for Excel).
    Any time: documents not read yet are in it with their status."""
    if format not in ("json", "csv"):
        raise HTTPException(400, "format must be json or csv")
    job = load_job(job_id)
    name = {"Content-Disposition": f'attachment; filename="ld-{job["job_id"][:8]}.{format}"'}
    if format == "csv":
        return Response(review.export_csv(job), media_type="text/csv; charset=utf-8", headers=name)
    return JSONResponse(review.export_json(job), headers=name)
