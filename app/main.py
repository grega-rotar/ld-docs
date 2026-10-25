"""HTTP API: POST /v1/extract (one document, waits for the answer) and /v1/batch (many, polled by job id)."""
import asyncio
import json
import os
import secrets
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .extract import BATCH_PARALLEL, READER_URL, ExtractError, extract
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


@app.get("/health")
def health():
    return {"ok": True, "reader": "local" if READER_URL else "claude"}


@app.post("/v1/extract", dependencies=[Depends(auth)])
async def extract_one(files: list[UploadFile] = File(...)):
    """One document: page 1 and page 2 as two files, or one multi-page TIFF/PDF."""
    docs = await documents(files)
    if len(docs) > 1:
        raise HTTPException(400, f"found {len(docs)} documents (pages {[d.pages[0].name for d in docs]}); "
                                 "send one document here, or use /v1/batch")
    try:
        return await extract(docs[0])
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
            job["documents"][i] = {"status": "done", **await extract(doc, batch=True, progress=progress_of(i))}
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
async def batch(files: list[UploadFile] = File(...)):
    """Many documents: loose pages (sorted by name, e.g. 00000001.tif, 00000002.tif …), multi-page files or a ZIP.
    Answers right away; the pages are split into documents (at the QR code on each page 1) in the background.
    Poll GET /v1/batch/{job_id}: status goes splitting -> running -> done (or error)."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup()
    job = {
        "job_id": uuid.uuid4().hex, "status": "splitting", "created_at": time.time(), "parallel": BATCH_PARALLEL,
        "split": {"done": 0, "total": 0}, "total": None, "done": 0, "documents": [],
    }  # fmt: skip
    _live[job["job_id"]] = job
    save(job)
    task = asyncio.create_task(follow_job(job, await read_uploads(files)))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {k: job[k] for k in ("job_id", "status", "total")}


@app.get("/v1/batch/{job_id}", dependencies=[Depends(auth)])
def batch_status(job_id: str):
    """While a job runs, each pending document has `stage`: queued, preparing, starting, page1, page2, thinking,
    writing (with `fields` / `of`) or retrying, and `started_at` once it has a slot."""
    if job_id in _live:
        return JSONResponse(_live[job_id])
    if not job_id.isalnum() or not job_path(job_id).exists():
        raise HTTPException(404, "no such job (results are deleted after JOB_TTL_DAYS)")
    job = json.loads(job_path(job_id).read_text())
    if job["status"] in ("splitting", "running"):
        job["status"] = "interrupted"  # the server restarted while it ran; send the files again
    return JSONResponse(job)
