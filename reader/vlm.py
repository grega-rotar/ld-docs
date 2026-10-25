"""The local vision model (Qwen3-VL through Ollama) for what only a model can read: free handwriting.

It gets one small crop per field, never a whole page, and answers in plain text: the handwriting, or
one of the words EMPTY, SIGNATURE, CROSSED_OUT. (A JSON schema made the 8B model pad every answer with
~150 whitespace tokens: 3 s instead of 0.3 s.) Fields that are measured to be empty never reach it, so
it has no blank space to invent text in.
"""
import base64
import io
import os

import httpx
import numpy as np
from PIL import Image

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("VLM_MODEL", "qwen3-vl:8b-instruct")
TIMEOUT = float(os.environ.get("VLM_TIMEOUT", "120"))
OPTIONS = {"temperature": 0, "num_ctx": 4096, "num_predict": 40}

PROMPTS = {
    "currency": "Handwritten currency on a form (a code like SEK or a name, any language). Transcribe the handwriting exactly.",
    "country": "Handwritten country name on a form (any language or script). Transcribe the handwriting exactly, with its accents.",
}
TAIL = " Answer with only the text. If it is a signature answer SIGNATURE, if it is struck through answer CROSSED_OUT, if there is nothing answer EMPTY."
WORDS = {"SIGNATURE": "signature", "CROSSED_OUT": "crossed_out", "EMPTY": "empty"}


def _png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def ask(field: str, crop: np.ndarray, prompt: str | None = None) -> dict:
    """{'kind': text|signature|crossed_out|empty, 'text'} for one field crop."""
    body = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": OPTIONS,
        "keep_alive": "24h",
        "messages": [{"role": "user", "content": (prompt or PROMPTS[field]) + TAIL, "images": [_png(Image.fromarray(crop))]}],
    }
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    text = (r.json().get("message", {}).get("content") or "").strip().strip("\"'`.").strip()
    kind = WORDS.get(text.upper().replace(" ", "_"))
    if kind:
        return {"kind": kind, "text": ""}
    return {"kind": "text" if text else "empty", "text": text}


def read_amount(crop: np.ndarray) -> str:
    """The handwritten digits in a row of dotted boxes, as the model sees them: '2.500,00'."""
    prompt = (
        "Handwritten digits in dotted boxes on a form, one digit per box, with a printed dot and comma between the groups. "
        "Write the number exactly as written, with the dot and the comma, e.g. 2.500,00. Ignore empty boxes. "
        "If no box has a digit, answer EMPTY."
    )
    body = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": {**OPTIONS, "num_predict": 16},
        "keep_alive": "24h",
        "messages": [{"role": "user", "content": prompt, "images": [_png(Image.fromarray(crop))]}],
    }
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return (r.json().get("message", {}).get("content") or "").strip()


def choose(field: str, crop: np.ndarray, options: list[str]) -> int | None:
    """Which option the handwriting says (index), or None for none of them. Recognising one of a few
    names is much easier for the model than spelling unfamiliar handwriting letter by letter."""
    listing = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    what = "currency" if field == "currency" else "country name"
    prompt = (
        f"The image shows a handwritten {what}. Which option is written? Compare letter by letter.\n"
        f"{listing}\n0. none of these\nAnswer with the number only."
    )
    body = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": {**OPTIONS, "num_predict": 4},
        "keep_alive": "24h",
        "messages": [{"role": "user", "content": prompt, "images": [_png(Image.fromarray(crop))]}],
    }
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    digits = "".join(c for c in r.json().get("message", {}).get("content", "") if c.isdigit())
    n = int(digits) if digits else 0
    return n - 1 if 1 <= n <= len(options) else None


def warm() -> None:
    """Load the model into the GPU now, so the first document doesn't wait 1-2 minutes for it."""
    httpx.post(
        f"{OLLAMA_URL}/api/generate", json={"model": MODEL, "keep_alive": "24h", "options": OPTIONS}, timeout=600
    ).raise_for_status()
