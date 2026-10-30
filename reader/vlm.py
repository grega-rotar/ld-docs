"""The local vision model for what only a model can read: free handwriting. Qwen3-VL through Ollama on a
GPU (VLM_API=ollama, the default), or any OpenAI-compatible server such as vLLM (VLM_API=openai, e.g.
Qwen3.6 on ZPIZ's OpenShift AI).

It gets one small crop per field, never a whole page, and answers in plain text: the handwriting, or
one of the words EMPTY, SIGNATURE, CROSSED_OUT. (A JSON schema made the 8B model pad every answer with
~150 whitespace tokens: 3 s instead of 0.3 s.) Fields that are measured to be empty never reach it, so
it has no blank space to invent text in.
"""
import base64
import io
import os
import re

import httpx
import numpy as np
from PIL import Image

API = os.environ.get("VLM_API", "ollama")  # ollama | openai
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
VLM_URL = os.environ.get("VLM_URL", "").rstrip("/")  # openai: the base URL ending in /v1
KEY = os.environ.get("VLM_KEY", "")
MODEL = os.environ.get("VLM_MODEL", "qwen3-vl:8b-instruct")
TIMEOUT = float(os.environ.get("VLM_TIMEOUT", "120"))
OPTIONS = {"temperature": 0, "num_ctx": 4096, "num_predict": 40}
# openai: tokens added to every answer's budget for a model that thinks before it answers (Qwen3.6 behind a
# gateway that drops chat_template_kwargs, such as Bifrost at ZPIZ). 0 for a model that answers right away.
THINKING_TOKENS = int(os.environ.get("VLM_THINKING_TOKENS", "0"))
# TLS to the openai server: the cluster's CA bundle, else the system CAs, else (last resort) no check.
VERIFY: str | bool = os.environ.get("VLM_CA_BUNDLE") or os.environ.get("VLM_VERIFY_TLS", "1") != "0"
if VERIFY is False and API == "openai":
    print("WARNING: VLM_VERIFY_TLS=0, the vision model's TLS certificate is not checked", flush=True)

PROMPTS = {
    "currency": "Handwritten currency on a form (a code like SEK or a name, any language). Transcribe the handwriting exactly.",
    "country": "Handwritten country name on a form (any language or script). Transcribe the handwriting exactly, with its accents.",
}
TAIL = " Answer with only the text. If it is a signature answer SIGNATURE, if it is struck through answer CROSSED_OUT, if there is nothing answer EMPTY."
WORDS = {"SIGNATURE": "signature", "CROSSED_OUT": "crossed_out", "EMPTY": "empty"}
THINK = re.compile(r"<think>.*?</think>", re.S)


class OutOfTokens(Exception):
    """The model used up max_tokens thinking and never answered."""


def _png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _headers() -> dict:
    return {"Authorization": f"Bearer {KEY}"} if KEY else {}


def _ollama(prompt: str, png: str, budget: int) -> str:
    body = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": {**OPTIONS, "num_predict": budget},
        "keep_alive": "24h",
        "messages": [{"role": "user", "content": prompt, "images": [png]}],
    }
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("message", {}).get("content") or ""


def _openai(prompt: str, png: str, budget: int) -> str:
    body = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": budget + THINKING_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}},
            {"type": "text", "text": prompt},
        ]}],
    }
    r = httpx.post(f"{VLM_URL}/chat/completions", json=body, headers=_headers(), timeout=TIMEOUT, verify=VERIFY)
    r.raise_for_status()
    choice = r.json()["choices"][0]
    text = THINK.sub("", choice["message"].get("content") or "").split("</think>")[-1]  # a stray closing tag
    if choice.get("finish_reason") == "length" and (not text.strip() or "<think>" in text):
        raise OutOfTokens(
            f"the vision model was still thinking after {budget + THINKING_TOKENS} tokens; raise VLM_THINKING_TOKENS"
        )
    return text


def _send(prompt: str, crop: np.ndarray, budget: int) -> str:
    """The model's raw answer to one prompt about one crop, at most `budget` tokens long."""
    png = _png(Image.fromarray(crop))
    return (_openai if API == "openai" else _ollama)(prompt, png, budget)


def ask(field: str, crop: np.ndarray, prompt: str | None = None) -> dict:
    """{'kind': text|signature|crossed_out|empty, 'text'} for one field crop."""
    text = _send((prompt or PROMPTS[field]) + TAIL, crop, 40).strip().strip("\"'`.").strip()
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
    return _send(prompt, crop, 16).strip()


def choose(field: str, crop: np.ndarray, options: list[str]) -> int | None:
    """Which option the handwriting says (index), or None for none of them. Recognising one of a few
    names is much easier for the model than spelling unfamiliar handwriting letter by letter."""
    listing = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    what = "currency" if field == "currency" else "country name"
    prompt = (
        f"The image shows a handwritten {what}. Which option is written? Compare letter by letter.\n"
        f"{listing}\n0. none of these\nAnswer with the number only."
    )
    digits = "".join(c for c in _send(prompt, crop, 4) if c.isdigit())
    n = int(digits) if digits else 0
    return n - 1 if 1 <= n <= len(options) else None


def warm() -> None:
    """Load the model into the GPU now, so the first document doesn't wait 1-2 minutes for it. (An openai
    server keeps its model loaded: nothing to do.)"""
    if API == "openai":
        return
    httpx.post(
        f"{OLLAMA_URL}/api/generate", json={"model": MODEL, "keep_alive": "24h", "options": OPTIONS}, timeout=600
    ).raise_for_status()


def problem() -> str | None:
    """Why the model can't answer now, None when it can: its server is unreachable or fails, or the model is not
    among Ollama's running models or the openai server's models."""
    try:
        if API == "openai":
            r = httpx.get(f"{VLM_URL}/models", headers=_headers(), timeout=5, verify=VERIFY)
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", [])]
        else:
            r = httpx.get(f"{OLLAMA_URL}/api/ps", timeout=5)
            ids = [m.get("name") for m in r.json().get("models", [])]
    except Exception as e:
        return f"vision model server: {type(e).__name__}: {e}"[:300]
    return None if MODEL in ids else f"VLM_MODEL {MODEL} is not among the server's models {ids}"[:300]
