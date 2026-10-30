"""READERS (local only at ZPIZ), the reader's health in /health, and the clerk's review + export of a batch."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import extract as ex
from app import main
from tests.test_app import poll, tif

KEY = {"Authorization": "Bearer k"}
JOB = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "API_KEYS", ["k"])
    monkeypatch.setattr(main, "JOBS_DIR", tmp_path)
    monkeypatch.setattr("app.pages.read_qr", lambda im: None)
    monkeypatch.setattr(ex, "_reader_health", {})
    return TestClient(main.app)


# --- C2: READERS


def test_readers_default_unchanged():
    assert ex.enabled_readers("", "") == ["claude"]
    assert ex.enabled_readers("", "http://reader:8100") == ["local", "claude"]
    assert ex.enabled_readers(" local , claude,local", "http://r") == ["local", "claude"]
    with pytest.raises(RuntimeError, match="unknown"):
        ex.enabled_readers("local,gpt", "http://r")


def importing(**env) -> subprocess.CompletedProcess:
    """Imports the app in a fresh interpreter: READERS is read at import time."""
    env = {k: v for k, v in os.environ.items() if k not in ("READERS", "READER_URL")} | env
    code = "from app import main, extract; print(extract.READERS)"
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=Path(__file__).parent.parent, capture_output=True, text=True)


def test_local_without_reader_url_refuses_to_start():
    r = importing(READERS="local")
    assert r.returncode != 0 and "READER_URL" in r.stderr
    r = importing(READERS="local", READER_URL="http://reader:8100")
    assert r.returncode == 0 and r.stdout.strip() == "['local']"


@pytest.fixture
def local_only(monkeypatch):
    """READERS=local: a fake reader, and claude fails loudly if anything asks for it."""
    async def fake_reader(doc, progress=ex.no_progress):
        return {k: None for k in ex.FIELDS} | {"case_number": "10344-1/2023", "amount": 2500.0, "model": "local (m)"}

    def no_claude():
        raise AssertionError("claude_bin called with READERS=local")

    monkeypatch.setattr(ex, "READERS", [ex.LOCAL])
    monkeypatch.setattr(ex, "DEFAULT_READER", ex.LOCAL)
    monkeypatch.setattr(ex, "READER_URL", "http://reader.test")
    monkeypatch.setattr(ex, "run_local", fake_reader)
    monkeypatch.setattr(ex, "claude_bin", no_claude)
    monkeypatch.setattr(ex.httpx, "get", lambda url, **kw: httpx.Response(200, json={"ok": True, "model_loaded": True, "model": "m"}))


def test_local_only(client, local_only):
    health = client.get("/health").json()
    assert health["readers"] == ["local"] and health["reader"] == "local"
    for reader in ("claude", "both"):
        r = client.post("/v1/batch", headers=KEY, files={"files": ("all.tif", tif(2))}, data={"reader": reader})
        assert r.status_code == 400, reader
        two = [("files", ("1.tif", tif())), ("files", ("2.tif", tif()))]
        assert client.post("/v1/extract", headers=KEY, files=two, data={"reader": reader}).status_code == 400
    with client:
        r = client.post("/v1/batch", headers=KEY, files={"files": ("all.tif", tif(4))})
        job = poll(client, r.json()["job_id"])
    assert job["reader"] == "local" and [d["status"] for d in job["documents"]] == ["done", "done"]


def test_claude_bin_refuses_when_not_enabled(monkeypatch):
    monkeypatch.setattr(ex, "READERS", [ex.LOCAL])
    with pytest.raises(ex.ExtractError, match="not enabled"):
        ex.claude_bin()


def test_health_local(client, local_only, monkeypatch):
    assert client.get("/health").json()["local"] == {"ok": True, "model": "m"}
    calls = []
    monkeypatch.setattr(ex.httpx, "get", lambda url, **kw: calls.append(url))
    assert client.get("/health").json()["local"]["ok"] is True and calls == []  # cached for 15 s

    monkeypatch.setattr(ex, "_reader_health", {})
    monkeypatch.setattr(ex.httpx, "get", lambda url, **kw: httpx.Response(200, json={"ok": True, "model_loaded": False, "model": "m", "error": "VLM_MODEL m is not among the server's models ['x']"}))
    assert client.get("/health").json()["local"] == {"ok": False, "model": "m", "error": "VLM_MODEL m is not among the server's models ['x']"}  # up, but the model isn't there


def test_health_reader_unreachable(client, monkeypatch):
    monkeypatch.setattr(ex, "READERS", [ex.LOCAL])
    monkeypatch.setattr(ex, "READER_URL", "http://127.0.0.1:9")  # nothing listens there
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["local"] == {"ok": False, "model": None, "error": "reader unreachable at http://127.0.0.1:9 (ConnectError)"}


def test_a_reader_timeout_is_not_retried(monkeypatch):
    """The reader is still reading: asking again would only queue a second copy behind the first."""
    calls = []

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def post(self, url, **kw):
            calls.append(url)
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(ex.httpx, "AsyncClient", Client)
    monkeypatch.setattr(ex, "READER_TIMEOUT", 900)
    with pytest.raises(ex.ExtractError, match="within 900 s") as e:
        asyncio.run(ex.run_local(SimpleNamespace(pages=[])))
    assert len(calls) == 1 and not e.value.exhausted


def test_health_without_local_has_no_local(client):
    assert "local" not in client.get("/health").json()


# --- C3: review and export


def doc(**values) -> dict:
    return {"status": "done", **{k: None for k in ex.FIELDS}, "document_id": None, "vk": None, "confidence": {},
            "uncertain_fields": [], "notes": None, "warnings": [], "pages": [], "model": "local", **values}  # fmt: skip


@pytest.fixture
def job(client):
    """A done job written straight to JOBS_DIR, as after a restart."""
    data = {"job_id": JOB, "status": "done", "created_at": 1.0, "reader": "local", "parallel": 3, "total": 3, "done": 2,
            "split": {"done": 6, "total": 6}, "documents": [
        doc(case_number="10344-123/2023", document_id="141", vk="968", amount=2500.0, currency="SEK", country="ŠVEDSKA",
            other_country=False, signature_present=True, uncertain_fields=["amount", "currency"], pages=["1.tif", "2.tif"]),
        doc(case_number="10301-044/2023", amount=310.5, currency="EUR", pages=["3.tif", "4.tif"]),
        {"status": "pending", "stage": "queued", "pages": ["5.tif", "6.tif"]},
    ]}  # fmt: skip
    main.job_path(JOB).write_text(json.dumps(data))
    return data


def patch(client, body, index=0, job_id=JOB, headers=KEY):
    return client.patch(f"/v1/batch/{job_id}/documents/{index}", headers=headers, json=body)


def correct(client):
    return patch(client, {"fields": {"amount": {"action": "corrected", "value": 2500.02}}})


def test_correct_field(client, job):
    r = correct(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["amount"] == 2500.02 and d["review"]["status"] == "open" and d["review"]["confirmed_at"] is None
    f = d["review"]["fields"]["amount"]
    assert {k: f[k] for k in ("action", "read", "value")} == {"action": "corrected", "read": 2500.0, "value": 2500.02}
    assert isinstance(f["at"], float)
    assert client.get(f"/v1/batch/{JOB}", headers=KEY).json()["documents"][0] == d
    assert json.loads(main.job_path(JOB).read_text())["documents"][0] == d

    # A second correction keeps the first read value; correcting back to it is a confirmation.
    d = patch(client, {"fields": {"amount": {"action": "corrected", "value": 2600}}}).json()
    assert d["review"]["fields"]["amount"]["read"] == 2500.0 and d["amount"] == 2600
    d = patch(client, {"fields": {"amount": {"action": "corrected", "value": 2500.0}}}).json()
    assert d["review"]["fields"]["amount"]["action"] == "confirmed" and d["amount"] == 2500.0


def test_confirm_document(client, job):
    correct(client)
    d = patch(client, {"confirm": True}).json()
    assert d["review"]["status"] == "confirmed" and d["review"]["confirmed_at"] > 0
    assert d["review"]["fields"]["currency"]["action"] == "confirmed" and d["review"]["fields"]["currency"]["value"] == "SEK"
    assert d["review"]["fields"]["amount"]["action"] == "corrected"  # confirming the document keeps the correction
    d = patch(client, {"confirm": False}).json()
    assert d["review"]["status"] == "open" and d["review"]["confirmed_at"] is None
    assert d["amount"] == 2500.02


def test_confirm_field_restores_read_value(client, job):
    correct(client)
    d = patch(client, {"fields": {"amount": {"action": "confirmed"}}}).json()
    assert d["amount"] == 2500.0 and d["review"]["fields"]["amount"]["action"] == "confirmed"


def test_null_and_booleans(client, job):
    d = patch(client, {"fields": {"currency": {"action": "corrected", "value": None},
                                  "other_country": {"action": "corrected", "value": True}}}).json()  # fmt: skip
    assert d["currency"] is None and d["other_country"] is True
    assert d["review"]["fields"]["other_country"] == d["review"]["fields"]["other_country"] | {"read": False, "value": True}


@pytest.mark.parametrize("body, named", [
    ({"fields": {"amount": {"action": "corrected", "value": "abc"}}}, "amount"),
    ({"fields": {"amount": {"action": "corrected", "value": True}}}, "amount"),  # a boolean is not a number
    ({"fields": {"other_country": {"action": "corrected", "value": 1}}}, "other_country"),
    ({"fields": {"currency": {"action": "corrected", "value": 5}}}, "currency"),
    ({"fields": {"amount": {"action": "corrected"}}}, "amount"),
    ({"fields": {"amount": {"action": "guessed"}}}, "amount"),
    ({"fields": {"foo": {"action": "confirmed"}}}, "foo"),
    ({"fields": {"document_id": {"action": "confirmed"}}}, "document_id"),  # from the QR code, not a reviewable field
    ({"confirm": "yes"}, "confirm"),
    ({"fields": [], "extra": 1}, "fields"),
])  # fmt: skip
def test_patch_rejects(client, job, body, named):
    r = patch(client, body)
    assert r.status_code == 400 and named in r.json()["detail"], r.text
    assert "review" not in json.loads(main.job_path(JOB).read_text())["documents"][0]  # nothing saved


def test_patch_errors(client, job):
    assert patch(client, {"confirm": True}, index=99).status_code == 404
    assert patch(client, {"confirm": True}, index=-1).status_code == 404
    assert patch(client, {"confirm": True}, index=2).status_code == 409  # pending
    assert patch(client, {"confirm": True}, job_id="nope").status_code == 404
    assert patch(client, {"confirm": True}, job_id="..%2F..%2Fetc").status_code == 404
    assert patch(client, {"confirm": True}, headers={}).status_code == 401


def test_patch_interrupted_job(client, job):
    """A job cut off by a restart: its done documents can still be reviewed, and it stays interrupted."""
    main.job_path(JOB).write_text(json.dumps(job | {"status": "running"}))
    assert correct(client).status_code == 200
    assert json.loads(main.job_path(JOB).read_text())["status"] == "running"
    got = client.get(f"/v1/batch/{JOB}", headers=KEY).json()
    assert got["status"] == "interrupted" and got["documents"][0]["amount"] == 2500.02


def test_patch_live_job(client, job, monkeypatch):
    """While the job runs, the review lands in the in-memory job that polling reads (and on disk)."""
    live = json.loads(main.job_path(JOB).read_text()) | {"status": "running"}
    monkeypatch.setitem(main._live, JOB, live)
    assert correct(client).status_code == 200
    assert live["documents"][0]["amount"] == 2500.02
    assert client.get(f"/v1/batch/{JOB}", headers=KEY).json()["status"] == "running"
    assert json.loads(main.job_path(JOB).read_text())["documents"][0]["amount"] == 2500.02


def test_export_json(client, job):
    correct(client)
    patch(client, {"confirm": True})
    r = client.get(f"/v1/batch/{JOB}/export?format=json", headers=KEY)
    assert r.status_code == 200 and r.headers["content-disposition"] == f'attachment; filename="ld-{JOB[:8]}.json"'
    body = r.json()
    assert body["job_id"] == JOB and body["exported_at"] > 0
    d0, d1, d2 = body["documents"]
    assert d0["amount"] == 2500.02 and d0["corrections"] == {"amount": {"read": 2500.0, "value": 2500.02}}
    assert d0["reviewed"] is True and d1["reviewed"] is False and d1["corrections"] == {}
    assert d2["status"] == "pending" and d2["amount"] is None and d2["pages"] == ["5.tif", "6.tif"]
    want = set(ex.FIELDS) | {"document_id", "vk", "pages", "status", "reviewed", "corrections", "uncertain_fields",
                             "notes", "warnings", "error"}  # fmt: skip
    assert all(set(d) == want for d in body["documents"])
    types = {"number": (int, float), "string": str, "boolean": bool}
    for d in body["documents"]:  # the fields keep ZPIZ's types (LDDokumentP1 / P2)
        assert all(d[k] is None or isinstance(d[k], types[t]) for k, t in ex.FIELDS.items())


def test_export_csv(client, job):
    correct(client)
    r = client.get(f"/v1/batch/{JOB}/export?format=csv", headers=KEY)
    assert r.status_code == 200 and r.headers["content-disposition"] == f'attachment; filename="ld-{JOB[:8]}.csv"'
    assert r.headers["content-type"].startswith("text/csv")
    assert r.content.startswith(b"\xef\xbb\xbf")
    lines = r.content.decode("utf-8-sig").splitlines()
    head = lines[0].split(";")
    assert lines[0].startswith("case_number;document_id;vk;")
    others = [k for k in ex.FIELDS if k != "case_number"]
    assert head == ["case_number", "document_id", "vk", *others,
                    "reviewed", "corrected_fields", "uncertain_fields", "pages", "status", "error"]  # fmt: skip
    assert len(lines) == 4
    row = dict(zip(head, lines[1].split(";")))
    assert ";2500,02;" in lines[1] and row["corrected_fields"] == "amount"
    assert row["uncertain_fields"] == "amount, currency" and row["pages"] == "1.tif, 2.tif"
    assert (row["country"], row["other_country"], row["signature_present"], row["reviewed"]) == ("ŠVEDSKA", "ne", "da", "ne")
    assert (row["amount1"], row["error"], row["status"]) == ("", "", "done")
    assert dict(zip(head, lines[2].split(";")))["amount"] == "310,50"
    assert dict(zip(head, lines[3].split(";")))["status"] == "pending"


def test_export_errors(client, job):
    assert client.get(f"/v1/batch/{JOB}/export?format=xml", headers=KEY).status_code == 400
    assert client.get("/v1/batch/nope/export?format=json", headers=KEY).status_code == 404
    assert client.get(f"/v1/batch/{JOB}/export?format=json").status_code == 401
