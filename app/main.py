"""HTTP API: POST /v1/extract (one document, waits for the answer) and /v1/batch (many, polled by job id)."""
import asyncio
import json
import os
import secrets
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .extract import ExtractError, extract
from .pages import group, load_pages, unpack

API_KEYS = [k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()]
JOBS_DIR = Path(os.environ.get("JOBS_DIR", "/data/jobs"))
JOB_TTL = int(os.environ.get("JOB_TTL_DAYS", "7")) * 86400  # results hold personal data: delete them after this

app = FastAPI(title="LD documents", version="1", docs_url="/docs", redoc_url=None)
bearer = HTTPBearer(auto_error=False)
_running: dict[str, asyncio.Task] = {}  # job_id -> task, so a job left "running" by a restart shows up


def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not API_KEYS:
        raise HTTPException(503, "no API_KEYS configured on the server")
    if not cred or not any(secrets.compare_digest(cred.credentials, k) for k in API_KEYS):
        raise HTTPException(401, "missing or wrong API key (Authorization: Bearer <key>)")


async def documents(files: list[UploadFile]):
    try:
        raw = unpack([(f.filename or "upload", await f.read()) for f in files])
        pages = [p for name, data in raw for p in load_pages(name, data)]
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e)) from None
    if not pages:
        raise HTTPException(400, "no pages in the upload")
    return await asyncio.to_thread(group, pages)


@app.get("/health")
def health():
    return {"ok": True}


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


async def run_job(job: dict, docs):
    async def one(i, doc):
        try:
            job["documents"][i] = {"status": "done", **await extract(doc, batch=True)}
        except Exception as e:
            job["documents"][i] = {"status": "error", "error": str(e), "pages": [p.name for p in doc.pages]}
        job["done"] += 1
        save(job)

    await asyncio.gather(*(one(i, d) for i, d in enumerate(docs)))
    job["status"] = "done"
    job["finished_at"] = time.time()
    save(job)


def cleanup():
    for f in JOBS_DIR.glob("*.json"):
        if time.time() - f.stat().st_mtime > JOB_TTL:
            f.unlink(missing_ok=True)


@app.post("/v1/batch", dependencies=[Depends(auth)], status_code=202)
async def batch(files: list[UploadFile] = File(...)):
    """Many documents: loose pages (sorted by name, e.g. 00000001.tif, 00000002.tif …), multi-page files or a ZIP.
    Pages are paired by the QR code on each page 1. Poll GET /v1/batch/{job_id} for the results."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup()
    docs = await documents(files)
    job = {
        "job_id": uuid.uuid4().hex, "status": "running", "created_at": time.time(), "total": len(docs), "done": 0,
        "documents": [{"status": "pending", "pages": [p.name for p in d.pages]} for d in docs],
    }  # fmt: skip
    save(job)
    _running[job["job_id"]] = asyncio.create_task(run_job(job, docs))
    _running[job["job_id"]].add_done_callback(lambda _: _running.pop(job["job_id"], None))
    return {k: job[k] for k in ("job_id", "status", "total")}


@app.get("/v1/batch/{job_id}", dependencies=[Depends(auth)])
def batch_status(job_id: str):
    if not job_id.isalnum() or not job_path(job_id).exists():
        raise HTTPException(404, "no such job (results are deleted after JOB_TTL_DAYS)")
    job = json.loads(job_path(job_id).read_text())
    if job["status"] == "running" and job_id not in _running:
        job["status"] = "interrupted"  # the server restarted while it ran; send the files again
    return JSONResponse(job)
