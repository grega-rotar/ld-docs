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

## Configuration (`/srv/ld-docs/.env` on the server)
| Variable | Default | |
|---|---|---|
| `API_KEYS` | (none, so every call gets 503) | comma-separated; one per client, e.g. one for ZPIZ |
| `CLAUDE_MODEL` | `sonnet` | `opus` reads messy handwriting a bit better but costs much more |
| `MAX_PARALLEL` | `6` | batch documents read at once (each claude run uses ~300 MB RAM) |
| `MAX_PARALLEL_SINGLE` | `4` | `/v1/extract` runs at once, on their own slots so they never wait behind a batch (Cloudflare cuts a request after ~100 s) |
| `JOB_TTL_DAYS` | `7` | |

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
