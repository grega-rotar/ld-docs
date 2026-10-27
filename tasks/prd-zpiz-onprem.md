# PRD: ld-docs on ZPIZ's own server

## Introduction

ld-docs reads ZPIZ "Izjava o višini tuje pokojnine" (LD) forms and returns ZPIZ's JSON. Today it runs on our server
(ld-docs.etiam.si), reads with Claude or with the local reader on a GPU laptop (Qwen3-VL 8B through Ollama), and the
upload page shows results but has no way to confirm or correct them.

ZPIZ wants to run it themselves. Scans must not leave their network, so this build:

- reads **only with the local reader**, whose vision model is **Qwen3.6-35B-A3B on ZPIZ's OpenShift AI cluster**
  (vLLM, OpenAI-compatible API) instead of Ollama on a GPU;
- runs as **two Docker containers on one ZPIZ Linux VM** (`docker compose up -d`), no GPU and no Cloudflare;
- gives clerks a **review workflow**: confirm or correct each flagged field, confirm the document, export the batch;
- is documented for **ZPIZ IT (install, operate)** and **clerks (use)**.

## Goals

- `docker compose up -d` on a VM with the `.env` filled in gives a working service at `http://<vm>:8080`.
- With `READERS=local`, no code path starts `claude` or contacts anything except the vLLM endpoint.
- A clerk can go through a 51-document batch, act on every flagged field, and download JSON/CSV with the corrections.
- Corrections survive a page reload and a server restart (stored in the job file).

## Decisions (from the user)

| | |
|---|---|
| Deploy | Docker compose on a ZPIZ VM (no OpenShift manifests) |
| Readers | Local only; Claude, claude-pool, comparison and costs not shown |
| Qwen3.6 role | Replaces Qwen3-VL inside the existing reader pipeline (crops only), nothing else changes |
| UI | Clerk-facing Slovenian wording, polish, review workflow |
| Design | `tasks/prototype-review-ui.html`, **variant A** ("Obrazec ob skenu") and the states below it |

## Contracts (all workstreams build against exactly these)

### C1. Reader → vision model (`reader/vlm.py`)

| Variable | Default | |
|---|---|---|
| `VLM_API` | `ollama` | `openai` = any OpenAI-compatible server (vLLM, OpenShift AI MaaS) |
| `VLM_URL` | (none) | base URL ending in `/v1`, e.g. `https://<model-route>.apps.<cluster>.<domain>/v1` |
| `VLM_KEY` | (none) | sent as `Authorization: Bearer …` (the service-account token or MaaS key) |
| `VLM_MODEL` | `qwen3-vl:8b-instruct` | `redhataiqwen36-35b-a3b` at ZPIZ |
| `VLM_CA_BUNDLE` | (system CAs) | path to a PEM with the cluster's CA |
| `VLM_VERIFY_TLS` | `1` | `0` turns verification off (last resort, logged as a warning at start) |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | unchanged, only for `VLM_API=ollama` |

`openai` request per call: `POST {VLM_URL}/chat/completions`,
`{"model", "temperature": 0, "max_tokens": <same budget as the Ollama num_predict of that call>,
"chat_template_kwargs": {"enable_thinking": false}, "messages": [{"role": "user", "content": [
{"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}, {"type": "text", "text": <prompt>}]}]}`.
Answer = `choices[0].message.content`, with any `<think>…</think>` removed. Reader `/health` with `openai`:
`GET {VLM_URL}/models`, `model_loaded` = `VLM_MODEL` is among the ids. `warm()` with `openai`: no-op.

### C2. API: which readers exist

`READERS` (comma list, optional). Unset: today's behaviour (`local,claude` with `READER_URL`, else `claude`).
`READERS=local` (ZPIZ): only `local`, `GET /health` → `"readers": ["local"]`, `reader=claude|both` → 400, no claude
binary is ever looked up or started. `local` in `READERS` without `READER_URL` → the app refuses to start.

`GET /health` additionally returns `"local": {"ok": bool, "model": str|null}` when `local` is enabled, from the
reader's `/health` (3 s timeout, cached 15 s); `ok` is `false` when the reader is unreachable or its model isn't loaded.

### C3. API: review

A document in a batch job (`status: "done"`) gets a `review` object once a clerk acts on it:

```json
"review": {
  "status": "open" | "confirmed",
  "confirmed_at": 1793131044.2 | null,
  "fields": { "amount": {"action": "corrected", "read": 2500.0, "value": 2500.02, "at": 1793131040.1},
              "currency": {"action": "confirmed", "read": "SEK", "value": "SEK", "at": 1793131041.0} }
}
```

`PATCH /v1/batch/{job_id}/documents/{index}` (bearer auth), JSON body
`{"fields": {"<field>": {"action": "confirmed"} | {"action": "corrected", "value": <typed>}}, "confirm": true|false}`:

- `<field>` must be one of the 17 fields in `FIELDS` (`app/extract.py`); `value` must have that field's type
  (`number`, `string`, `boolean`) or be `null` → else **400** with the field named.
- A correction sets the document's top-level field to `value`; `read` keeps the first value the reader produced.
  Correcting a field back to its `read` value stores `action: "confirmed"`.
- `confirm: true` sets `review.status = "confirmed"` and `confirmed_at`; every field in `uncertain_fields` not yet in
  `review.fields` is added as `confirmed`. `confirm: false` re-opens (`status: "open"`, `confirmed_at: null`).
- Unknown job **404**, index out of range **404**, document not `done` **409**. Returns the whole updated document.
- Saved to the job file at once (and to the in-memory job while it still runs); `GET /v1/batch/{id}` returns it.

`GET /v1/batch/{job_id}/export?format=json|csv` (bearer auth), any time (documents not yet done are included with
their status):

- `json`: `{"job_id", "exported_at", "documents": [ {<the 17 FIELDS>, "document_id", "vk", "pages", "status",
  "reviewed": bool, "corrections": {"<field>": {"read", "value"}}, "uncertain_fields", "notes", "warnings",
  "error"} … ]}`, `Content-Disposition: attachment; filename="ld-<job_id[:8]>.json"`. Each element validates as
  `LDDokumentP1`/`LDDokumentP2` (extra keys ignored).
- `csv`: UTF-8 with BOM, `;` separator, decimal comma (Slovenian Excel), one row per document, columns
  `case_number;document_id;vk;<the other FIELDS in FIELDS order>;reviewed;corrected_fields;uncertain_fields;pages;status;error`
  (lists joined with `, `, booleans `da`/`ne`, null empty). `filename="ld-<job_id[:8]>.csv"`.
- Unknown format **400**, unknown job **404**.

### C4. Deployment (`deploy/zpiz/`)

`compose.yml` with `api` (`ghcr.io/grega-rotar/ld-docs:${IMAGE_TAG:-latest}`, port `${HTTP_PORT:-8080}:8000`, volume
`ld_data:/data`) and `reader` (`ghcr.io/grega-rotar/ld-reader:${IMAGE_TAG:-latest}`, no published port, reachable as
`http://reader:8100`), both `restart: unless-stopped`, healthchecks, json-file log rotation. One `.env`
(`.env.example` committed): `API_KEYS`, `READERS=local`, `READER_URL=http://reader:8100`, `READER_KEY` (=`READER_KEYS`),
`VLM_API=openai`, `VLM_URL`, `VLM_KEY`, `VLM_MODEL`, optional `VLM_CA_BUNDLE` (mounted `./ca.pem`), `JOB_TTL_DAYS`,
`LOCAL_PARALLEL`, `READER_PARALLEL`. CI pushes `ld-reader` to GHCR next to `ld-docs`.

## User stories

### US-1: The reader uses Qwen3.6 on the cluster
**Acceptance criteria**
- [reader] With `VLM_API=openai`, `vlm.ask("country", crop)` sends one POST to `{VLM_URL}/chat/completions` with
  `Authorization: Bearer <VLM_KEY>`, `chat_template_kwargs.enable_thinking == false`, `temperature == 0`, an
  `image_url` part whose URL starts with `data:image/png;base64,`, and returns `{"kind": "text", "text": "ŠVEDSKA"}`
  for the answer content `"ŠVEDSKA"`.
- [reader] Content `"<think>x</think>EMPTY"` → `{"kind": "empty", "text": ""}`.
- [reader] `read_amount` / `choose` send `max_tokens` 16 / 4; `choose` answer `"2"` with 3 options → index 1.
- [reader] With `VLM_API=ollama`, the existing tests in `tests/reader/test_reader.py` pass unchanged.
- [reader] `GET /health` with `openai` and a stubbed `/models` listing `redhataiqwen36-35b-a3b` → `model_loaded: true`;
  `/models` failing → `model_loaded: false`, still 200.
- [reader] A 5xx from the vision model on `/v1/read` → 503 `vision model unavailable`.

### US-2: ZPIZ build reads with the local reader only
- [api] `READERS=local`, `READER_URL` set: `/health` → `readers == ["local"]`; `POST /v1/batch` with `reader=claude`
  → 400; with no `reader` the job's `reader` is `local`; `claude_bin` is never called (monkeypatched to fail).
- [api] `READERS=local` without `READER_URL` → importing/starting the app raises.
- [api] `READERS` unset: every existing test in `tests/test_app.py` passes unchanged.
- [api] `/health` with the reader stub answering `{"ok": true, "model_loaded": true, "model": "m"}` →
  `local == {"ok": true, "model": "m"}`; reader unreachable → `local.ok == false`.

### US-3: Confirm and correct fields, confirm the document
- [api] On a done job whose document 0 has `amount: 2500.0` and `uncertain_fields: ["amount", "currency"]`,
  PATCH `{"fields": {"amount": {"action": "corrected", "value": 2500.02}}}` → 200, `amount == 2500.02`,
  `review.fields.amount == {"action": "corrected", "read": 2500.0, "value": 2500.02, …}`, `review.status == "open"`;
  a following `GET /v1/batch/{id}` shows the same; the job file on disk holds it.
- [api] PATCH `{"confirm": true}` then → `review.status == "confirmed"`, `review.fields.currency.action == "confirmed"`.
- [api] `{"fields": {"amount": {"action": "corrected", "value": "abc"}}}` → 400 naming `amount`; `{"fields": {"foo": …}}`
  → 400; index 99 → 404; a `pending` document → 409; no key → 401.
- [browser] Variant A: in document `10344-123/2023` the flagged `Znesek` shows an input with `2.500,00`, buttons
  `Pravilno` and `Shrani popravek`. Typing `2.500,02` + `Shrani popravek` → the field shows `2.500,02` in pen blue
  with `2.500,00` struck through; reload → still so.
- [browser] Typing `2500.0O` + `Shrani popravek` → `Znesek zapišite s številkami, npr. 2.500,02.` under the input,
  no request sent.
- [browser] `Potrdi dokument in naprej →` (or `Enter` outside an input) → the queue tag becomes `Potrjeno`, the
  counter goes from `38 od 51` to `39 od 51`, the next document is selected.

### US-4: Filter the queue
- [browser] With a batch of 51 (13 with flagged fields, 0 confirmed), `Za preverbo 13` shows exactly 13 items (not 51),
  `Potrjeni 0` shows the empty state `Ni dokumentov …` with `Pokaži vse`; after confirming one, the counts read
  `Za preverbo 12` / `Potrjeni 1`.

### US-5: Export
- [api] `export?format=json` after the US-3 correction → `documents[0].amount == 2500.02`,
  `documents[0].corrections.amount == {"read": 2500.0, "value": 2500.02}`, `reviewed == true` after confirm; header
  `attachment; filename="ld-<8>.json"`.
- [api] `export?format=csv` → body starts with BOM, header row starts `case_number;document_id;vk;`, the row for
  document 0 contains `;2500,02;` and `amount` in `corrected_fields`; `format=xml` → 400.
- [browser] `JSON za ZPIZ` / `CSV` download `ld-<8>.json` / `ld-<8>.csv`; with unreviewed documents the hint
  `N dokumentov še ni pregledanih …` is shown above the buttons.

### US-6: States
- [browser] Start: no key → drop zone disabled with `Najprej se prijavite s ključem.`; wrong key (401) →
  `Ključ ni pravilen. Nov ključ dobite pri skrbniku sistema.`
- [browser] Reading: queue items show the real stage (`v vrsti`, `bere stran 1`, `bere rokopis`), the open document
  shows the step list and skeleton.
- [browser] `/health` `local.ok == false` → header `Bralnik ne deluje` (red dot) and the banner with `Poskusi znova`.
- [browser] A document with `status: error` shows the error box with the server's message; the queue tag is red.
- [browser] All documents confirmed → the summary (`Vseh 51 dokumentov je pregledanih`, counts) with the export choice.
- [browser] Header shows no Claude/Primerjava switch, no tokens or cost anywhere when `readers == ["local"]`.

### US-7: Deploy and docs
- [ops] `docker compose -f deploy/zpiz/compose.yml config` succeeds with `.env.example` copied to `.env`.
- [ops] CI builds and pushes `ghcr.io/grega-rotar/ld-reader:<sha>` and `:latest` on main.
- [docs] `docs/zpiz-namestitev.md` (Slovenian, for ZPIZ IT): requirements, install, every `.env` variable, CA bundle,
  token expiry check, update and rollback (`IMAGE_TAG`), offline install (`docker save`/`load`), data and deletion,
  logs, health, troubleshooting, accuracy check (`reader.evaluate`) before go-live.
- [docs] `docs/navodila-za-referente.md` (Slovenian, for clerks): upload, what yellow means, confirm/correct,
  keyboard, export, what to do on errors.
- [docs] `README.md` links both and documents C1–C3 (English).

## Functional requirements

- FR-1: `VLM_API=openai` client as in C1; Ollama path unchanged.
- FR-2: `READERS` as in C2; `/health.local` as in C2.
- FR-3: Review PATCH and export as in C3, persisted in the job file.
- FR-4: UI = prototype variant A + states; clerk wording; no cost/tokens/dev block; reader switch hidden with one reader.
- FR-5: `deploy/zpiz/` as in C4; CI pushes the reader image.
- FR-6: Docs as in US-7.

## Non-goals

- No OpenShift manifests, no SSO/user accounts (one API key per client stays), no Claude at ZPIZ.
- No change to how the reader finds fields, its confidence rules or the digit CNN.
- No whole-page reading by Qwen3.6.
- No database: job files on the volume, deleted after `JOB_TTL_DAYS`.

## Design considerations

- **Prototype:** `tasks/prototype-review-ui.html` (local file) — **chosen variant: A**. The online canvas was not kept.
- Reuse `app/ui.html`'s tokens, fonts, TIFF preview (UTIF), ZIP handling and polling.

## Technical considerations

- Qwen3.6 has not been called from here (the cluster is only reachable inside ZPIZ). Prompts were tuned on Qwen3-VL 8B;
  `reader.evaluate` on the 51 samples must be run at ZPIZ before go-live (docs say how).
- The MaaS URL form (`…/<project>/<model>/v1`) is also just a `VLM_URL`.
- Service-account tokens expire; the docs show how to check `exp`.

## Success metrics

- A clerk reviews a 51-document batch (56 flagged fields) in under 15 minutes.
- Zero calls leave the ZPIZ network besides the vLLM endpoint.

## Open questions

- Does ZPIZ's VM reach ghcr.io, or do they need the offline install?
- Which CA signs the cluster's `*.apps` routes?
- Should exported JSON go somewhere automatically (ZPIZ system API) later?

## Verification checklist

### Per-change coverage
- **vLLM client**: payload, auth, think-strip, health, 503 — `tests/reader/test_vlm.py`.
- **READERS**: local-only health/400/no claude, refuse without READER_URL, defaults unchanged — `tests/test_review.py`.
- **Review PATCH**: correct, confirm, validation 400s, 404/409/401, persistence on disk — `tests/test_review.py`.
- **Export**: JSON shape + corrections, CSV BOM/separator/decimal comma, 400 — `tests/test_review.py`.
- **UI**: driven in a browser against the API with the reader stubbed (US-3…US-6).

### Test data
A done job written straight to `JOBS_DIR` by the tests: 3 documents — `10344-123/2023` (`amount 2500.0`,
`currency "SEK"`, `uncertain_fields ["amount","currency"]`), `10301-044/2023` (nothing flagged), one `pending`.

### Suite gates
- [ ] `pytest tests --ignore=tests/reader` in the API image passes.
- [ ] `pytest tests/reader` in the reader image passes.
- [ ] `docker compose -f deploy/zpiz/compose.yml config` passes.
- [ ] Browser walkthrough of US-3…US-6, zero console errors.
