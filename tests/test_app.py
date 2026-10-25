"""Pairing, parsing and the HTTP layer, with claude stubbed out (the real reading is checked with scripts/run_local.py)."""
import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import extract as ex
from app import main
from app.pages import Page, group, load_pages, natural_key, unpack


def tif(pages=1) -> bytes:
    ims = [Image.new("1", (200, 300), 1) for _ in range(pages)]
    buf = io.BytesIO()
    ims[0].save(buf, "TIFF", save_all=True, append_images=ims[1:])
    return buf.getvalue()


def test_natural_sort_and_zip():
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("10.tif", b"b")
        f.writestr("2.tif", b"a")
        f.writestr("__MACOSX/._2.tif", b"x")
    assert [n for n, _ in unpack([("x.zip", z.getvalue()), ("1.tif", b"c")])] == ["1.tif", "2.tif", "10.tif"]
    assert sorted(["00000010", "00000002"], key=natural_key) == ["00000002", "00000010"]


def test_multipage_tiff_and_bad_file():
    pages = load_pages("doc.tif", tif(2))
    assert [p.name for p in pages] == ["doc.tif#1", "doc.tif#2"]
    assert pages[0].image.mode == "1"
    with pytest.raises(ValueError):
        load_pages("x.txt", b"hello")


def test_group_by_qr(monkeypatch):
    qr = {"a1": {"document_id": "1", "vk": "9"}, "b1": {"document_id": "2", "vk": "8"}}
    monkeypatch.setattr("app.pages.read_qr", lambda im: qr.get(im.info["n"]))

    def pages(*names):
        out = []
        for n in names:
            im = Image.new("1", (1, 1))
            im.info["n"] = n
            out.append(Page(n, im))
        return out

    # b2 missing, c1's QR unreadable: c1 still starts a new document because a1+a2 is full.
    docs = group(pages("a1", "a2", "b1", "c1", "c2"))
    assert [[p.name for p in d.pages] for d in docs] == [["a1", "a2"], ["b1", "c1"], ["c2"]]
    assert docs[0].qr == qr["a1"]


def test_clean():
    raw = {k: None for k in ex.FIELDS} | {"amount1": 12, "currency1": " eur ", "other_country": "yes", "amount": True}
    out = ex.clean(raw)
    assert (out["amount1"], out["currency1"], out["other_country"], out["amount"]) == (12, "EUR", None, None)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "API_KEYS", ["k"])
    monkeypatch.setattr(main, "JOBS_DIR", tmp_path)
    monkeypatch.setattr("app.pages.read_qr", lambda im: None)

    async def fake_claude(workdir, files, progress=ex.no_progress):
        assert files == ["p1.png", "p2.png"][: len(files)]
        for stage in ("starting", "page1", "page2", "thinking"):
            progress(stage)
        progress("writing", fields=3, of=len(ex.FIELDS))
        return {k: None for k in ex.FIELDS} | {"case_number": "10344-1/2023", "amount": 2500.5, "uncertain_fields": ["amount", "bogus"],
                                              "confidence": {k: 0.99 for k in ex.FIELDS} | {"amount": 0.7, "currency": 1.5}}, "m", \
            {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 1500, "cost_usd": 0.006}

    monkeypatch.setattr(ex, "run_claude", fake_claude)
    return TestClient(main.app)


def test_auth(client):
    assert client.post("/v1/extract", files={"files": ("a.tif", tif())}).status_code == 401
    r = client.post("/v1/extract", headers={"Authorization": "Bearer nope"}, files={"files": ("a.tif", tif())})
    assert r.status_code == 401


def test_extract_one(client):
    r = client.post("/v1/extract", headers={"Authorization": "Bearer k"}, files=[("files", ("1.tif", tif())), ("files", ("2.tif", tif()))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["case_number"] == "10344-1/2023" and body["amount"] == 2500.5
    assert body["uncertain_fields"] == ["amount"]
    assert body["pages"] == ["1.tif", "2.tif"]
    assert body["warnings"] == ["no QR code found on page 1: document_id and vk are missing"]
    assert list(body)[:3] == ["case_number", "document_id", "vk"]
    assert body["usage"]["cost_usd"] == 0.006 and body["usage"]["duration_ms"] >= 0
    conf = body["confidence"]
    assert list(conf)[:3] == ["case_number", "document_id", "vk"] and len(conf) == len(ex.FIELDS) + 2
    assert (conf["amount"], conf["currency"], conf["document_id"]) == (0.7, 1.0, None)  # clamped; no QR -> None


def test_extract_one_rejects_many(client):
    files = [("files", (f"{i}.tif", tif())) for i in range(4)]
    r = client.post("/v1/extract", headers={"Authorization": "Bearer k"}, files=files)
    assert r.status_code == 400 and "/v1/batch" in r.json()["detail"]


def poll(client, job_id):
    for _ in range(200):
        job = client.get(f"/v1/batch/{job_id}", headers={"Authorization": "Bearer k"}).json()
        if job["status"] not in ("splitting", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job still {job['status']}")


def test_batch_bad_upload(client):
    with client:
        r = client.post("/v1/batch", headers={"Authorization": "Bearer k"}, files={"files": ("notes.txt", b"hello")})
        assert r.status_code == 202
        job = poll(client, r.json()["job_id"])
    assert job["status"] == "error" and "notes.txt" in job["error"]


def test_batch(client):
    with client:
        r = client.post("/v1/batch", headers={"Authorization": "Bearer k"}, files={"files": ("all.tif", tif(4))})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        assert r.json()["status"] == "splitting"
        job = poll(client, job_id)
    assert job["total"] == job["done"] == 2
    assert [d["pages"] for d in job["documents"]] == [["all.tif#1", "all.tif#2"], ["all.tif#3", "all.tif#4"]]
    assert all(d["status"] == "done" and "stage" not in d for d in job["documents"])
    assert job["split"] == {"done": 4, "total": 4} and job["parallel"] >= 1
    assert client.get("/v1/batch/nope", headers={"Authorization": "Bearer k"}).status_code == 404
    assert json.loads(next(main.JOBS_DIR.glob("*.json")).read_text())["status"] == "done"


def test_ui(client):
    r = client.get("/")
    assert r.status_code == 200 and "/v1/batch" in r.text


def test_usage_of():
    out = {"total_cost_usd": 0.0123456, "modelUsage": {
        "a": {"inputTokens": 3, "outputTokens": 400, "cacheReadInputTokens": 1000, "cacheCreationInputTokens": 50},
        "b": {"inputTokens": 1, "outputTokens": 2}}}
    assert ex.usage_of(out) == {"input_tokens": 4, "output_tokens": 402, "cache_read_tokens": 1000,
                                "cache_write_tokens": 50, "cost_usd": 0.01235}


def test_extract_local(client, monkeypatch):
    """READER_URL set: the pages go to the local reader at full resolution; the QR code is still read here."""
    sent = {}

    async def fake_reader(doc, progress=ex.no_progress):
        progress("preparing")
        sent["pages"] = [ex.page_png(p) for p in doc.pages[:2]]
        progress("reading")
        return {k: None for k in ex.FIELDS} | {
            "case_number": "10344-1/2023", "amount": 2500.5, "country": "ŠVEDSKA", "country_code": "SE",
            "confidence": {k: 0.99 for k in ex.FIELDS} | {"amount": 0.75}, "uncertain_fields": ["amount"],
            "notes": "stran 2, znesek: števke prebrane različno", "warnings": ["page 1: yes/no boxes not found"],
            "margin": {"document_id": "999", "vk": None}, "model": "local (qwen3-vl:8b-instruct)",
        }  # fmt: skip

    monkeypatch.setattr(ex, "READER_URL", "https://reader.example")
    monkeypatch.setattr(ex, "run_local", fake_reader)
    qrs = iter([{"document_id": "141", "vk": "968"}, None])  # page 1 has the QR code, page 2 none
    monkeypatch.setattr("app.pages.read_qr", lambda im: next(qrs))
    r = client.post("/v1/extract", headers={"Authorization": "Bearer k"}, files=[("files", ("1.tif", tif())), ("files", ("2.tif", tif()))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["document_id"], body["vk"], body["amount"], body["country_code"]) == ("141", "968", 2500.5, "SE")
    assert body["confidence"]["document_id"] == 1.0 and body["confidence"]["amount"] == 0.75
    assert body["uncertain_fields"] == ["amount"] and body["model"].startswith("local")
    assert "page 1: yes/no boxes not found" in body["warnings"]
    assert any("differs from the margin text (999)" in w for w in body["warnings"])
    assert body["usage"]["cost_usd"] == 0.0
    assert Image.open(io.BytesIO(sent["pages"][0])).size == (200, 300)  # not shrunk


def test_health_says_which_reader(client, monkeypatch):
    assert client.get("/health").json()["reader"] == "claude"
    monkeypatch.setattr(main, "READER_URL", "https://reader.example")
    assert client.get("/health").json()["reader"] == "local"
