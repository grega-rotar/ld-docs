"""The vision model client (reader/vlm.py) against an OpenAI-compatible server (vLLM), with httpx stubbed.

Contract C1 / US-1 in tasks/prd-zpiz-onprem.md. The Ollama path is covered by test_reader.py.
"""
import io

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from reader import server, vlm

URL = "https://qwen.example/v1"
CROP = np.full((40, 120), 255, dtype=np.uint8)


@pytest.fixture
def openai(monkeypatch):
    """VLM_API=openai; every POST is recorded and answered with `answer.content` (or `answer.status`)."""
    monkeypatch.setattr(vlm, "API", "openai")
    monkeypatch.setattr(vlm, "VLM_URL", URL)
    monkeypatch.setattr(vlm, "KEY", "tok")
    monkeypatch.setattr(vlm, "MODEL", "redhataiqwen36-35b-a3b")
    answer = {"content": "", "status": 200, "finish_reason": "stop", "calls": []}

    def post(url, json=None, headers=None, **kw):
        answer["calls"].append({"url": url, "json": json, "headers": headers or {}, **kw})
        message = {"role": "assistant", "content": answer["content"]}
        body = {"choices": [{"message": message, "finish_reason": answer["finish_reason"]}]}
        return httpx.Response(answer["status"], json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(vlm.httpx, "post", post)
    return answer


def test_ask_sends_one_openai_chat_request(openai):
    openai["content"] = "ŠVEDSKA"
    assert vlm.ask("country", CROP) == {"kind": "text", "text": "ŠVEDSKA"}
    [call] = openai["calls"]
    assert call["url"] == f"{URL}/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer tok"
    body = call["json"]
    assert body["model"] == "redhataiqwen36-35b-a3b"
    assert body["temperature"] == 0
    assert body["max_tokens"] == 40
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    image, text = body["messages"][0]["content"]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/png;base64,")
    assert text["type"] == "text" and text["text"].startswith(vlm.PROMPTS["country"])


def test_think_blocks_are_removed(openai):
    openai["content"] = "<think>x</think>EMPTY"
    assert vlm.ask("country", CROP) == {"kind": "empty", "text": ""}
    openai["content"] = "<think>\nhmm\n</think>\n\nSEK"
    assert vlm.ask("currency", CROP) == {"kind": "text", "text": "SEK"}


def test_read_amount_and_choose_budgets(openai):
    openai["content"] = "2.500,00"
    assert vlm.read_amount(CROP) == "2.500,00"
    openai["content"] = "2"
    assert vlm.choose("country", CROP, ["ŠVEDSKA", "ŠVICA", "ŠPANIJA"]) == 1
    assert [c["json"]["max_tokens"] for c in openai["calls"]] == [16, 4]


def test_thinking_tokens_are_added_to_every_budget(openai, monkeypatch):
    monkeypatch.setattr(vlm, "THINKING_TOKENS", 4000)
    openai["content"] = "<think>a long thought</think>SEK"
    assert vlm.ask("currency", CROP) == {"kind": "text", "text": "SEK"}
    openai["content"] = "2"
    vlm.choose("country", CROP, ["ŠVEDSKA", "ŠVICA"])
    assert [c["json"]["max_tokens"] for c in openai["calls"]] == [4040, 4004]


@pytest.mark.parametrize("content", ["", None, "<think>still thinking", "<think>done</think>  "])
def test_cut_off_while_thinking_raises(openai, content):
    openai["content"], openai["finish_reason"] = content, "length"
    with pytest.raises(vlm.OutOfTokens, match="VLM_THINKING_TOKENS"):
        vlm.ask("currency", CROP)


def test_a_short_answer_at_the_limit_is_kept(openai):
    openai["content"], openai["finish_reason"] = "3", "length"
    assert vlm.choose("country", CROP, ["A", "B", "C"]) == 2


def test_tls_settings_reach_httpx(openai, monkeypatch):
    monkeypatch.setattr(vlm, "VERIFY", "/certs/ca.pem")
    openai["content"] = "SEK"
    vlm.ask("currency", CROP)
    assert openai["calls"][0]["verify"] == "/certs/ca.pem"


def test_warm_is_a_no_op(openai, monkeypatch):
    monkeypatch.setattr(vlm.httpx, "post", lambda *a, **k: pytest.fail("warm() must not call an openai server"))
    vlm.warm()


def test_a_5xx_raises_an_http_error(openai):
    openai["status"] = 503
    with pytest.raises(httpx.HTTPError):
        vlm.ask("country", CROP)


def models(monkeypatch, response):
    seen = []

    def get(url, headers=None, **kw):
        seen.append((url, headers))
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json=response, request=httpx.Request("GET", url))

    monkeypatch.setattr(vlm.httpx, "get", get)
    return seen


def test_health_lists_the_model(openai, monkeypatch):
    seen = models(monkeypatch, {"object": "list", "data": [{"id": "redhataiqwen36-35b-a3b", "object": "model"}]})
    r = TestClient(server.app).get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "redhataiqwen36-35b-a3b", "model_loaded": True}
    assert seen == [(f"{URL}/models", {"Authorization": "Bearer tok"})]


def test_health_with_another_model_or_none(openai, monkeypatch):
    models(monkeypatch, {"data": [{"id": "qwen3.6/redhataiqwen36-35b-a3b"}]})
    body = TestClient(server.app).get("/health").json()
    assert body["model_loaded"] is False and "qwen3.6/redhataiqwen36-35b-a3b" in body["error"]
    models(monkeypatch, httpx.ConnectError("refused"))
    r = TestClient(server.app).get("/health")
    assert r.status_code == 200 and r.json()["model_loaded"] is False and "ConnectError" in r.json()["error"]


def test_read_answers_503_when_the_model_server_fails(openai, monkeypatch):
    openai["status"] = 500
    monkeypatch.setattr(server, "KEYS", ["k"])
    monkeypatch.setattr(server, "read_document", lambda pages: {"country": vlm.ask("country", CROP)})
    png = io.BytesIO()
    Image.new("L", (50, 50), 255).save(png, format="PNG")
    r = TestClient(server.app).post(
        "/v1/read", headers={"Authorization": "Bearer k"}, files=[("files", ("p1.png", png.getvalue(), "image/png"))]
    )
    assert r.status_code == 503
    assert r.json()["detail"].startswith("vision model unavailable")


def test_read_answers_503_when_the_model_runs_out_of_tokens(openai, monkeypatch):
    openai["content"], openai["finish_reason"] = None, "length"
    monkeypatch.setattr(server, "KEYS", ["k"])
    monkeypatch.setattr(server, "read_document", lambda pages: {"country": vlm.ask("country", CROP)})
    png = io.BytesIO()
    Image.new("L", (50, 50), 255).save(png, format="PNG")
    r = TestClient(server.app).post(
        "/v1/read", headers={"Authorization": "Bearer k"}, files=[("files", ("p1.png", png.getvalue(), "image/png"))]
    )
    assert r.status_code == 503 and "VLM_THINKING_TOKENS" in r.json()["detail"]


def test_ollama_is_the_default_and_unchanged(monkeypatch):
    calls = []

    def post(url, json=None, **kw):
        calls.append((url, json, kw))
        return httpx.Response(200, json={"message": {"content": "SEK"}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(vlm.httpx, "post", post)
    assert vlm.API == "ollama"
    assert vlm.ask("currency", CROP) == {"kind": "text", "text": "SEK"}
    [(url, body, kw)] = calls
    assert url == f"{vlm.OLLAMA_URL}/api/chat" and "verify" not in kw and "headers" not in kw
    assert body["think"] is False and body["options"]["num_predict"] == 40 and body["keep_alive"] == "24h"
    assert isinstance(body["messages"][0]["images"][0], str)
