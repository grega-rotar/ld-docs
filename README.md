# ld-docs

API at **https://ld-docs.etiam.si** that reads ZPIZ "Izjava o višini tuje pokojnine" (LD) forms and returns the JSON ZPIZ asked for (`LDDokumentP1` + `LDDokumentP2`), linked to the case.

- Page 1 is recognised by its ZPIZ QR code. The code gives `document_id` and `vk` exactly; they are not OCR guesses.
- Claude (through claude-pool) reads the case number, the ticked boxes and the handwriting. Each document is one locked-down `claude -p` run: it may only `Read` its two page images, and it has to answer in a JSON schema (`app/extract.py`, prompt in `app/prompt.md`).
- The forms can be in any language or script (Swedish, French, Serbian Cyrillic …). The layout is the same.

## API

A simple upload page is at `/`: enter the API key, drop the scans, and download the results as JSON. Every `/v1` call needs `Authorization: Bearer <API key>`. Interactive docs are at `/docs`.

### One document: `POST /v1/extract`
Send page 1 and page 2 as two files, or one multi-page TIFF/PDF. TIFF, PNG, JPEG and PDF are accepted. The call waits for the answer (usually 15–30 s).

```bash
curl -H "Authorization: Bearer $KEY" \
  -F files=@00000001.tif -F files=@00000002.tif \
  https://ld-docs.etiam.si/v1/extract
```

```json
{
  "case_number": "10344-123/2023",
  "document_id": "140000001",
  "vk": "123456789",
  "documentation_number": "0100001",
  "beneficiary_number": "1001000011",
  "other_country": true,
  "amount1": null, "currency1": null, "country1": null,
  "amount2": null, "currency2": null, "country2": null,
  "amount3": null, "currency3": null, "country3": null,
  "amount": 2500.0, "currency": "SEK", "country": "ŠVEDSKA",
  "signature_present": false,
  "confidence": {
    "case_number": 0.99, "document_id": 1.0, "vk": 1.0, "documentation_number": 0.99, "beneficiary_number": 0.99,
    "other_country": 0.98, "amount1": 0.99, "currency1": 0.99, "country1": 0.99, "...": "...",
    "amount": 0.7, "currency": 0.96, "country": 0.93, "signature_present": 0.97
  },
  "uncertain_fields": ["amount"],
  "notes": "decimals are written as two open loops: read as 00, could be 02",
  "warnings": [],
  "pages": ["00000001.tif", "00000002.tif"],
  "model": "claude-sonnet-5-5"
}
```

- **Link to the case:** `case_number` (zadeva, from the header), `document_id` + `vk` (from the QR code, the same as `Št. dok. … ID: … VK: …` on the page margin), plus `documentation_number` and `beneficiary_number` from the header.
- **ZPIZ's 14 fields**, with the types of `LDDokumentP1` / `LDDokumentP2`. Extra keys are ignored by a default pydantic model, so `LDDokumentP1.model_validate(result)` works as it is.
  - `currency1-3` / `country1-3`: the code next to the ticked box (`EUR`, `HRK` …, `D`, `A`, `BIH` …).
  - `currency` (page 2): the ISO code when the handwriting clearly means one (`SEK`), otherwise as written.
  - `country` (page 2): as written by hand (`ŠVEDSKA`).
  - `other_country`: `null` when neither yes nor no is ticked.
- **`confidence`**: one number from 0 to 1 per field, the probability that the value (including `null`, meaning "empty") is exactly right. `document_id` and `vk` are 1.0 because they come from the QR code; they are `null` if there was no QR code. Every other number is Claude's own estimate, not a calibrated statistic. On the samples, clear print scored 0.98–0.99, clear handwriting 0.9–0.97, and ambiguous digits 0.5–0.85. A sensible rule: have a person check anything below 0.9.
- **`usage`**: `duration_ms` (reading time, excluding queueing), `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, and `cost_usd`, which is what the run would cost on Anthropic API billing (claude-pool pays a subscription instead). With Sonnet, a document is typically ~10 s, ~16k tokens and ~$0.04–0.05.
- **For a human to check:** `uncertain_fields` lists the fields the model wasn't sure about, and `notes` says why. `warnings` lists technical problems: no QR code, a missing page 2, more than 2 pages.

Errors: `400` for a bad upload or more than one document (use the batch), `401` for a wrong key, `502` when Claude fails, `503` when every claude-pool account is at its usage limit.

### Many documents: `POST /v1/batch` + `GET /v1/batch/{job_id}`
Send loose pages (they are sorted by name, so `00000001.tif`, `00000002.tif` … work), multi-page files, or one ZIP.
Pages are split into documents at each QR code, so a missing page 2 doesn't shift every later document.
If a QR code can't be read, it falls back to odd = page 1 and even = page 2.

```bash
curl -H "Authorization: Bearer $KEY" -F files=@ld_test.zip https://ld-docs.etiam.si/v1/batch
# {"job_id": "3f2a…", "status": "splitting", "total": null}     <- answers as soon as the upload is in
curl -H "Authorization: Bearer $KEY" https://ld-docs.etiam.si/v1/batch/3f2a…
# {"status": "splitting", "split": {"done": 46, "total": 102}, ...}
# {"status": "running", "parallel": 6, "done": 12, "total": 51,
#  "documents": [{"status": "done", ...same fields as /v1/extract...},
#                {"status": "pending", "stage": "writing", "fields": 9, "of": 17, "started_at": 1792880414.2, "pages": [...]},
#                {"status": "pending", "stage": "queued", "pages": [...]},
#                {"status": "error", "error": "...", "pages": [...]}, ...]}
```
- `status`: `splitting` (pages are being split at the QR codes; `split` counts them) → `running` → `done`. Other values: `error` (the upload couldn't be read; see `error`) and `interrupted` (the server restarted during the job, so send the files again).
- Each pending document has a `stage` that reports what Claude is actually doing, not a timer: `queued` → `preparing` → `starting` → `page1` → `page2` → `thinking` → `writing` (with `fields`/`of` as the answer streams in). `retrying` means a run failed and is being retried once.
- Documents come back in page order. Six are read at a time (`MAX_PARALLEL`); at ~10 s each, 51 documents take about 1½ minutes.

Results are kept for `JOB_TTL_DAYS` (7), then deleted, because they hold personal data.

## Local reader (no Claude, no per-document cost)
With `READER_URL` set, the API sends each document to `reader/` instead of Claude: a service on a machine with
an NVIDIA GPU (a laptop with an RTX 4080 12 GB is enough), reached through a Cloudflare Tunnel. The API, the
upload page and the JSON stay the same; `model` says `local (…)` and `usage.cost_usd` is 0.

How it reads a document, in any language and any version of the form. Nothing is placed by fixed coordinates:
- **Layout** (`layout.py`, `page1.py`, `page2.py`): the page is straightened, checkboxes are found by shape, dotted
  digit cells from their dots, writing lines from their strokes. Rows are told apart by the codes printed next to
  the boxes (read with Tesseract, `labels.py`), so a new currency list or order is read, not assumed. The three
  blocks on page 1 repeat the same rows, so a box hidden under a heavy mark is put back from the others.
- **Ticks** (`marks.py`): measured as ink inside the box. Two ticks, a blacked-out box or a stray stroke are reported,
  never guessed.
- **Header** (`header.py`): the case number by its shape, checked against the sideways copy in the margin; the
  beneficiary number usually contains the documentation number, which confirms both.
- **Amounts** (`digits.py`, `amount.py`): a small CNN reads each cell, the vision model reads the whole row; they
  have to agree.
- **Page 2 handwriting** (`handwriting.py`, `names.py`, `vlm.py`): Qwen3-VL 8B (Ollama, ~6 GB of GPU memory) reads
  the currency and the country, then picks among the closest real names from every language in CLDR. The currency
  has to be one the country used. Empty fields are measured empty and never shown to the model.
- **Confidence** comes from agreement and cross-checks, not from a model's own estimate: anything below 0.9 is in
  `uncertain_fields` with a reason in `notes` (in Slovenian). On ZPIZ's 51 sample documents: see "Accuracy" below.

### Run it on the GPU machine
```bash
# once: NVIDIA driver + Docker with GPU support (WSL: Docker Desktop or the NVIDIA Container Toolkit)
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi   # must list the GPU
cp reader/.env.example reader/.env       # READER_KEYS, TUNNEL_TOKEN
docker compose -f reader/compose.yml up -d --build   # first start pulls the model (6 GB)
```
`reader/.env` (mode 600, never committed):

| Variable | |
|---|---|
| `READER_KEYS` | comma-separated keys the API must send (`Authorization: Bearer …`); generate with `openssl rand -hex 32` |
| `TUNNEL_TOKEN` | the Cloudflare Tunnel's token (Zero Trust → Networks → Tunnels → create → Docker); its public hostname (e.g. `ld-reader.etiam.si`) points to `http://reader:8100` |
| `VLM_MODEL` | optional, default `qwen3-vl:8b-instruct` |
| `READER_PARALLEL` | optional, documents read at once, default 2 |

Nothing listens on the machine's own network: the tunnel is the only way in, and every call needs a key.
On the server, set `READER_URL=https://ld-reader.etiam.si` and `READER_KEY=<one of READER_KEYS>` in
`/srv/ld-docs/.env` and restart. Without `READER_URL` the API uses Claude as before.
If the reader is down, `/v1/extract` answers 503 and batch documents get `status: error`; send them again later.

### Local, Claude, or both
With `READER_URL` set, both readers are available (`GET /health` lists them in `readers`). The local one is the
default; a call picks another with the form field `reader`:

```bash
curl -H "Authorization: Bearer $KEY" -F reader=both -F files=@ld_test.zip https://ld-docs.etiam.si/v1/batch
```

`reader=both` reads every document with both at once and returns the local result plus `compare`:
`{"reader": "claude", "differences": ["amount", …], "result": {…Claude's whole result…}}`, or
`{"reader": "claude", "error": "…"}` if Claude failed (if the local reader failed, the answer is Claude's and
`compare` holds the local error). Amounts are compared to the cent; names ignore case and accents. In the upload
page, the switch next to the API key does the same: "Primerjava" shows each field Claude read differently under it.
Each reader keeps its own slots, so a comparison is as fast as the slower of the two.

### Check accuracy on scans
```bash
# reference: a /v1/batch result for the same scans (e.g. from the Claude version), saved as results/ref.json
docker run --rm --network host -v "$PWD:/w" -w /w -e PYTHONPATH=/w ld-reader:dev \
  python -m reader.evaluate ld_test results/ref.json --out results/local.json
```
It prints every difference and, most important, the fields that were **wrong while claiming ≥ 0.9** (exit code 1 if
any). Run it after any change to `reader/` and on new kinds of scans before trusting them.

### Accuracy
ZPIZ's 51 sample documents (102 pages, Swedish, English, German, French, Italian, Serbian, Croatian, Macedonian,
Slovenian forms of 2021 and 2023), compared field by field with the Claude version's answers; the digit model
used is the public one, which never saw these scans:

| | |
|---|---|
| fields agreeing with the reference | 857 / 867 (98.8 %) |
| **wrong while claiming ≥ 0.9** | **0** |
| sent to a person (< 0.9) | 56 fields (6.5 %): mostly amounts, see below |
| checkboxes (currency, country, yes/no), header numbers, signature, page 2 currency/country | all agree; the doubtful ones flagged |
| time per document | 4–15 s on an RTX 4080 Laptop (12 GB) |

Every one of the 10 disagreements is a handwritten amount that was flagged (e.g. a final `2` written like a
reversed C, a `3` like a J). Amounts are flagged often on purpose: a digit counts as sure only when the CNN and the
vision model read the same and the CNN is at full certainty for every digit (`CELL_SURE` in `amount.py`). Real-cell
accuracy of the digit CNN alone is 96.7 % (public model), 97.3 % cross-validated after fine-tuning on these scans.
Loosen `CELL_SURE` only after `reader.evaluate` on more checked scans says it is safe.

### Digit model
`reader/models/digits.onnx` is trained on MNIST + EMNIST only (public data), augmented to look like pen digits cut
out of the cells, so it can sit in this public repo:
`docker build -t ld-train -f reader/training/Dockerfile . && docker run --rm -v "$PWD:/w" -v digitdata:/tmp/digit-data -w /w ld-train python -m reader.training.train_digits` (~30 min on a CPU).
On the GPU machine it can then learn the local handwriting from checked scans, into the git-ignored
`reader/models/digits.local.onnx`, which the reader prefers when present:
```bash
python -m reader.training.cells_from_scans ld_test results/ref.json results/cells.npz   # in ld-reader:dev
python -m reader.training.finetune_digits results/cells.npz                             # in ld-train, prints cross-validated accuracy
```

## Configuration (`/srv/ld-docs/.env` on the server)
| Variable | Default | |
|---|---|---|
| `API_KEYS` | (none, so every call gets 503) | comma-separated; one per client, e.g. one for ZPIZ |
| `CLAUDE_MODEL` | `sonnet` | `opus` reads messy handwriting a bit better but costs much more |
| `MAX_PARALLEL` | `6` | batch documents Claude reads at once (each claude run uses ~300 MB RAM) |
| `MAX_PARALLEL_SINGLE` | `4` | `/v1/extract` runs at once per reader, on their own slots so they never wait behind a batch (Cloudflare cuts a request after ~100 s) |
| `JOB_TTL_DAYS` | `7` | |
| `READER_URL` | (none: Claude reads) | the local reader, e.g. `https://ld-reader.etiam.si` (see "Local reader"); it becomes the default reader |
| `LOCAL_PARALLEL` | `3` | batch documents sent to the local reader at once |
| `READER_KEY` | | one of the reader's `READER_KEYS` |

## Development
```bash
docker build -t ld-docs:dev .
docker run --rm -p 127.0.0.1:8077:8000 -e API_KEYS=devkey -e CLAUDE_POOL_SERVER_ID=local \
  -v ~/.config/claude-pool:/home/app/.config/claude-pool:ro ld-docs:dev
# tests (claude stubbed out):
docker run --rm -v "$PWD:/src:ro" -w /src --entrypoint sh ld-docs:dev -c \
  "pip install -q --user --break-system-packages -r requirements-dev.txt && python3 -m pytest -q -p no:cacheprovider tests"
```
`ld_test/` (ZPIZ's sample scans) holds personal data and is git-ignored. Never commit scans or results.

## Deploy
Same setup as rokomet-stats: push to `main` → GitHub Actions runs the tests in the image → pushes `ghcr.io/grega-rotar/ld-docs:<sha>` → SSHes to t4a as `deploy` and runs `docker compose up -d` in `/srv/ld-docs` → waits for `https://ld-docs.etiam.si/health`.
Rollback: run the workflow by hand with `tag=<older sha>`.

One-time setup:
1. Repo secrets `DEPLOY_HOST`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` (the same values as rokomet-stats).
2. On t4a: `/srv/ld-docs` (owner `deploy`) with `.env` (`API_KEYS=…`, mode 600) and `claude-pool/` (copy of `/srv/rokomet-stats/claude-pool`, mode 600, owner uid 1000).
3. After the first image push: make the GHCR package `ld-docs` public (package settings), so t4a can pull without logging in.
4. Cloudflare: `A ld-docs.etiam.si → 77.42.42.60`, proxied. The `*.etiam.si` origin certificate on the edge Traefik already covers it.
