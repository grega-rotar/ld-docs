"""The local reader as an HTTP service, for the API server to call (through a Cloudflare Tunnel).

    POST /v1/read   pages as files (page 1, then page 2), Authorization: Bearer $READER_KEYS
    GET  /health    ok, and whether the vision model answers

One document at a time on the GPU: the vision model runs calls one after another anyway, and the
image work before it is a second or two of CPU per page.
"""
import asyncio
import io
import os
import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image

from . import names, vlm
from .document import read_document

KEYS = [k.strip() for k in os.environ.get("READER_KEYS", "").split(",") if k.strip()]
PARALLEL = int(os.environ.get("READER_PARALLEL", "2"))  # documents read at once (CPU work overlaps the GPU)
SLOTS = asyncio.Semaphore(PARALLEL)
bearer = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not KEYS:
        raise RuntimeError("READER_KEYS is not set")
    await asyncio.to_thread(names._index)  # every country/currency name in ~700 languages, ~2 s
    try:
        await asyncio.to_thread(vlm.warm)  # load the model now, not on the first document
    except Exception as e:  # Ollama still starting: the first document loads it instead
        print("vision model not loaded yet:", e, flush=True)
    yield


app = FastAPI(title="LD reader", lifespan=lifespan, docs_url=None, redoc_url=None)


def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    if cred is None or not any(secrets.compare_digest(cred.credentials, k) for k in KEYS):
        raise HTTPException(401, "wrong or missing reader key")


@app.get("/health")
async def health() -> dict:
    loaded = await asyncio.to_thread(vlm.loaded)
    return {"ok": True, "model": vlm.MODEL, "model_loaded": loaded}


@app.post("/v1/read", dependencies=[Depends(auth)])
async def read(files: list[UploadFile] = File(...)) -> dict:
    if not 1 <= len(files) <= 2:
        raise HTTPException(400, "send page 1 and (optionally) page 2 of one document")
    try:
        pages = [Image.open(io.BytesIO(await f.read())) for f in files]
        for p in pages:
            p.load()
    except Exception as e:
        raise HTTPException(400, f"not an image: {e}") from None
    async with SLOTS:
        try:
            out = await asyncio.to_thread(read_document, pages)
        except httpx.HTTPError as e:
            raise HTTPException(503, f"vision model unavailable: {e}") from None
    out["model"] = f"local ({vlm.MODEL})"
    return out
