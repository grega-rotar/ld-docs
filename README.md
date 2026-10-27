# ld-docs

Reads ZPIZ "Izjava o višini tuje pokojnine" (LD) forms from scans and returns the JSON ZPIZ needs
(`LDDokumentP1` + `LDDokumentP2`), linked to the case. Clerks check the fields it wasn't sure about in a web page,
correct them, and export the batch as JSON or CSV.

- Page 1 is recognised by its ZPIZ QR code, which gives `document_id` and `vk` exactly (not OCR guesses).
- Ticked boxes, printed numbers and handwriting are read field by field. Every field gets a confidence, and anything
  below 0.9 is highlighted for a clerk to check.
- Forms in any language or script (Swedish, French, Serbian Cyrillic …) and both versions of the form (2021, 2023)
  are read the same way.

Guides in Slovenian: **[namestitev in delovanje (IT)](docs/zpiz-namestitev.md)** ·
**[navodila za referente](docs/navodila-za-referente.md)**

**Contents:** [How it runs at ZPIZ](#how-it-runs-at-zpiz) · [Docker images](#docker-images) ·
[Install](#install) · [Settings](#settings-env) · [Before go-live](#before-go-live-accuracy-check) ·
[Using it](#using-it) · [Operation](#operation) · [Troubleshooting](#troubleshooting) · [API](#api)

## How it runs at ZPIZ

```
 clerks' browsers ──HTTPS──▶ ZPIZ reverse proxy ──▶ Linux VM (docker compose)
                                                    ┌──────────────────────────────────────────────┐
                                                    │ api     web page + /v1 API        :8080       │
                                                    │   │     results in volume ld_data (7 days)    │
                                                    │   ▼                                           │
                                                    │ reader  OpenCV · Tesseract · digit model, CPU │
                                                    └───┬──────────────────────────────────────────┘
                                                        │ HTTPS: small crops of handwritten fields only
                                                        ▼
                                  OpenShift AI (vLLM) · Qwen3.6-35B-A3B · OpenAI-compatible API
```

- Two containers on one VM, no GPU. Only `api` has a port; `reader` is reachable only from `api`.
- Scans never leave the VM. The reader sends the vision model one small crop per handwritten field (amount, currency,
  country), never a whole page. Nothing goes to the internet.
- Results hold personal data and are deleted after `JOB_TTL_DAYS` (7). Uploaded files are not kept after splitting.

## Docker images

Built from every commit on `main` and published on GitHub Container Registry, public (no login needed):

| Image | What it is | Versions |
|---|---|---|
| `ghcr.io/grega-rotar/ld-docs` | `api`: the web page and the API | [package page](https://github.com/grega-rotar/ld-docs/pkgs/container/ld-docs) |
| `ghcr.io/grega-rotar/ld-reader` | `reader`: reads the forms | [package page](https://github.com/grega-rotar/ld-docs/pkgs/container/ld-reader) |

Tags: `latest` (newest) and the full git commit sha of each build. Both images of one commit have the same tag, so
one `IMAGE_TAG` picks a version. In production, pin a sha.

## Install

You need:
- a Linux VM with Docker Engine and the compose plugin; 2–4 vCPU, 4 GB RAM and 10 GB disk are a good start (estimate);
- outbound HTTPS from the VM to the model's route on OpenShift AI, and from the clerks' network to the VM;
- the model's URL (ending in `/v1`), its token (service-account token or MaaS key) and its model id.

```bash
sudo mkdir -p /opt/ld-docs/certs && cd /opt/ld-docs
curl -fsSLO https://raw.githubusercontent.com/grega-rotar/ld-docs/main/deploy/zpiz/compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/grega-rotar/ld-docs/main/deploy/zpiz/.env.example
chmod 600 .env
vi .env                         # the settings below
docker compose config --quiet   # names any required setting that is still missing
docker compose up -d            # pulls both images and starts them
docker compose ps               # both "healthy" after ~30 s
curl -s http://127.0.0.1:8080/health
# {"ok": true, "reader": "local", "readers": ["local"], "job_ttl_days": 7,
#  "local": {"ok": true, "model": "redhataiqwen36-35b-a3b"}}
```

Open `http://<vm>:8080`, sign in with one of the `API_KEYS` and drop a few scans. `"local": {"ok": false}` means the
reader can't reach the model: see [Troubleshooting](#troubleshooting).

For HTTPS, put it behind ZPIZ's reverse proxy and set `HTTP_BIND=127.0.0.1`. The proxy must allow uploads of
~200 MB and a 300 s read timeout (an nginx example is in the [IT guide](docs/zpiz-namestitev.md)).

The VM has no access to ghcr.io? Pull and `docker save` the two images on another machine, copy the file over and
`docker load` it (step by step in the IT guide).

## Settings (`.env`)

| Variable | Default | |
|---|---|---|
| `IMAGE_TAG` | `latest` | a git sha from the [package page](https://github.com/grega-rotar/ld-docs/pkgs/container/ld-docs) pins a version |
| `HTTP_BIND` / `HTTP_PORT` | `0.0.0.0` / `8080` | where the web page and the API listen |
| `API_KEYS` | required | comma-separated, one per client (clerks, each other system); `openssl rand -hex 32` |
| `READER_KEY` | required | shared by the two containers; `openssl rand -hex 32` |
| `VLM_API` | `openai` | the model's API type (OpenAI-compatible) |
| `VLM_URL` | required | the model's base URL, ending in `/v1`; the MaaS form `…/<project>/<model>/v1` works too |
| `VLM_KEY` | required | the model's bearer token |
| `VLM_MODEL` | `redhataiqwen36-35b-a3b` | the model id, as listed by `GET {VLM_URL}/models` |
| `VLM_CA_BUNDLE` | (system CAs) | `/certs/ca.pem` after putting the cluster's CA into `certs/ca.pem` |
| `VLM_VERIFY_TLS` | `1` | `0` turns TLS verification off (only temporarily) |
| `VLM_TIMEOUT` | `120` | seconds to wait for one answer of the model |
| `JOB_TTL_DAYS` | `7` | days before results are deleted |
| `LOCAL_PARALLEL` / `READER_PARALLEL` | `3` / `2` | documents read at once; about one per vCPU |

After changing `.env`: `docker compose up -d`.

Check the model from the VM before starting (the IT guide also has a test with an image):
```bash
set -a; . ./.env; set +a
curl -s ${VLM_CA_BUNDLE:+--cacert certs/ca.pem} -H "Authorization: Bearer $VLM_KEY" "$VLM_URL/models"   # must list VLM_MODEL
```

## Before go-live: accuracy check

The reader's prompts were tuned with Qwen3-VL 8B, where on ZPIZ's 51 sample documents 857 of 867 fields agreed with
the reference and no field was wrong while claiming ≥ 0.9. With Qwen3.6 this has to be measured again, on the VM:

```bash
# scans/ld_test = the sample scans, scans/ref.json = their checked results (we provide it)
docker compose run --rm -v "$PWD/scans:/w" reader python -m reader.evaluate /w/ld_test /w/ref.json --out /w/local.json
```
It prints every difference and, most important, the fields that were **wrong while claiming ≥ 0.9** (exit code 1 if
there are any). Go live only when that list is empty. Repeat it after changing the model or on new kinds of scans.
Scans and results hold personal data: delete `scans/` afterwards.

## Using it

Clerks open the page, sign in with their key and drop scans: loose pages (`00000001.tif`, `00000002.tif` …),
multi-page TIFF/PDF, or a ZIP of a whole batch. Pages are split into documents at each QR code. Each document then
shows its fields next to the scan, with the fields to check in yellow:
- **Pravilno** confirms a value;
- **Shrani popravek** saves a correction (the value that was read is kept too);
- **Potrdi dokument in naprej** confirms the document and opens the next one;
- **JSON za ZPIZ** and **CSV** (for Excel) export the batch with the corrections.

Reviews are saved on the server, so a reload or a restart keeps them. Full guide for clerks:
**[docs/navodila-za-referente.md](docs/navodila-za-referente.md)**.

## Operation

| | |
|---|---|
| Health | `GET /health` (no key): `local.ok` is `false` when the reader or the model doesn't answer; the page then shows "Bralnik ne deluje" |
| Logs | `docker compose logs -f api reader`; rotated at 5 × 10 MB per container |
| Update | in `.env` set `IMAGE_TAG` to the new sha, then `docker compose pull && docker compose up -d` |
| Roll back | the same with the previous sha; reviews and corrections are kept (volume `ld_data`) |
| Restart during a batch | that batch shows as `interrupted` and has to be uploaded again; update when nobody is reading |
| Backups | not needed: the volume holds only results of the last `JOB_TTL_DAYS` days |
| Keys | one `API_KEYS` entry per client; remove one to revoke it. `.env` mode 600, never in git |
| Model token | service-account tokens expire; check the date with the command in the IT guide and renew in time |

## Troubleshooting

| What you see | Cause | What to do |
|---|---|---|
| "Bralnik ne deluje", `local.ok: false` | reader down, or it can't reach the model | `docker compose ps`, `docker compose logs reader`, the `/models` check above |
| `401` from the model in the reader log | `VLM_KEY` wrong or expired | new token in `.env`, `docker compose up -d` |
| `CERTIFICATE_VERIFY_FAILED` | the cluster's CA is unknown | `certs/ca.pem` and `VLM_CA_BUNDLE=/certs/ca.pem` |
| "Ključ ni pravilen" | the key isn't in `API_KEYS` | add it, `docker compose up -d` |
| documents with "vision model unavailable" | the model on OpenShift AI isn't answering | check it in OpenShift AI; upload those documents again |
| `413` on large uploads | reverse proxy limit | raise `client_max_body_size` |
| slow reading | little CPU or a busy model | more vCPU, higher `READER_PARALLEL` / `LOCAL_PARALLEL` |

More cases in the [IT guide](docs/zpiz-namestitev.md#težave).

## API

For other systems. Every `/v1` call needs `Authorization: Bearer <one of API_KEYS>`; interactive docs are at
`http://<vm>:8080/docs`. `$LD` below is the server's address.

### One document: `POST /v1/extract`
Page 1 and page 2 as two files, or one multi-page TIFF/PDF (TIFF, PNG, JPEG, PDF). Waits for the answer.

```bash
curl -H "Authorization: Bearer $KEY" -F files=@00000001.tif -F files=@00000002.tif $LD/v1/extract
```
```json
{
  "case_number": "10344-123/2023", "document_id": "140000001", "vk": "123456789",
  "documentation_number": "0100001", "beneficiary_number": "1001000011",
  "other_country": true,
  "amount1": null, "currency1": null, "country1": null,
  "amount2": null, "currency2": null, "country2": null,
  "amount3": null, "currency3": null, "country3": null,
  "amount": 2500.0, "currency": "SEK", "country": "ŠVEDSKA", "signature_present": false,
  "confidence": {"case_number": 0.99, "document_id": 1.0, "vk": 1.0, "...": "...", "amount": 0.7},
  "uncertain_fields": ["amount"],
  "notes": "znesek: števke prebrane različno (2.500,02 / 2.500,00)",
  "warnings": [],
  "pages": ["00000001.tif", "00000002.tif"],
  "model": "local (redhataiqwen36-35b-a3b)"
}
```
- **Link to the case:** `case_number` (from the header), `document_id` + `vk` (from the QR code, the same as
  `Št. dok. … ID: … VK: …` on the margin), `documentation_number`, `beneficiary_number`.
- **ZPIZ's fields** with the types of `LDDokumentP1` / `LDDokumentP2` (extra keys are ignored, so
  `LDDokumentP1.model_validate(result)` works as it is). `currency1-3` / `country1-3`: the code next to the ticked box;
  `currency` (page 2): the ISO code when the handwriting clearly means one; `country` (page 2): as written;
  `other_country`: `null` when neither box is ticked.
- **`confidence`** per field, from agreement and cross-checks (e.g. the digit model and the vision model must read the
  same amount), not from a model's own estimate. Below 0.9 → in `uncertain_fields`, with the reason in `notes`.
- `warnings`: technical problems (no QR code, a missing page 2, more than 2 pages).

Errors: `400` bad upload or more than one document (use the batch), `401` wrong key, `503` reader or model unavailable.

### Many documents: `POST /v1/batch` + `GET /v1/batch/{job_id}`
Loose pages (sorted by name), multi-page files or one ZIP; answers at once, then poll.
```bash
curl -H "Authorization: Bearer $KEY" -F files=@paket.zip $LD/v1/batch       # {"job_id": "3f2a…", "status": "splitting"}
curl -H "Authorization: Bearer $KEY" $LD/v1/batch/3f2a…                     # status, done/total, documents[]
```
`status`: `splitting` → `running` → `done` (or `error`; `interrupted` after a restart: upload again). Each document
is `{"status": "done", …same fields as /v1/extract…}`, `pending` with its `stage`, or `error` with `error`.

### Review and export
```bash
curl -X PATCH -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"fields": {"amount": {"action": "corrected", "value": 2500.02}, "currency": {"action": "confirmed"}}, "confirm": true}' \
  $LD/v1/batch/3f2a…/documents/0
curl -OJ -H "Authorization: Bearer $KEY" "$LD/v1/batch/3f2a…/export?format=json"   # ld-3f2a….json
curl -OJ -H "Authorization: Bearer $KEY" "$LD/v1/batch/3f2a…/export?format=csv"    # ld-3f2a….csv
```
- PATCH: each field `{"action": "confirmed"}` or `{"action": "corrected", "value": …}`; `"confirm": true` confirms the
  document (`false` re-opens it). The document gets `review` with what was read and what was corrected. `400` for an
  unknown field or a wrong type, `404` unknown job or index, `409` document not done.
- `json` export: every document with the corrections applied, plus `reviewed` and `corrections`
  (`{"amount": {"read": 2500.0, "value": 2500.02}}`). `csv`: for Slovenian Excel (UTF-8 with BOM, `;`, decimal
  comma), one row per document.

---
Developing ld-docs: [docs/maintainers.md](docs/maintainers.md).
