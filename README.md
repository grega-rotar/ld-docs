# ld-docs

Reads ZPIZ "Izjava o višini tuje pokojnine" (LD) forms from scans and returns the JSON ZPIZ needs
(`LDDokumentP1` + `LDDokumentP2`). Clerks check the fields it wasn't sure about (shown in yellow), correct them,
and export the batch as JSON or CSV.

It runs on one Linux VM as two Docker containers, `api` (web page and API) and `reader`, with no GPU. For handwriting
the reader asks Qwen3.6 on ZPIZ's OpenShift AI, sending only small crops of the handwritten fields. Scans never
leave the VM.

## Install

You need a Linux VM with Docker (2–4 vCPU, 4 GB RAM), network access from it to the model on OpenShift AI, and the
model's URL, token and name.

```bash
sudo mkdir -p /opt/ld-docs && cd /opt/ld-docs
curl -fsSLO https://raw.githubusercontent.com/grega-rotar/ld-docs/main/deploy/zpiz/compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/grega-rotar/ld-docs/main/deploy/zpiz/.env.example
chmod 600 .env
vi .env                  # fill in the settings below
docker compose up -d     # downloads the images and starts them
curl -s http://127.0.0.1:8080/health    # "local": {"ok": true, …} means it can read
```

Both containers use the VM's own network, so open the port in its firewall
(`sudo firewall-cmd --add-port=8080/tcp --permanent && sudo firewall-cmd --reload`). Then open `http://<vm>:8080`
and sign in with one of the `API_KEYS`.

The images are public on GitHub: [ld-docs](https://github.com/grega-rotar/ld-docs/pkgs/container/ld-docs) and
[ld-reader](https://github.com/grega-rotar/ld-docs/pkgs/container/ld-reader).

## Settings (`.env`)

| Variable | |
|---|---|
| `API_KEYS` | keys for signing in, comma-separated, one per client; make each with `openssl rand -hex 32` |
| `READER_KEY` | a key between the two containers; `openssl rand -hex 32` |
| `VLM_URL` | the model's address, ending in `/v1` |
| `VLM_KEY` | the model's token |
| `VLM_MODEL` | the model's name, as `GET {VLM_URL}/models` lists it, e.g. `qwen3.6/redhataiqwen36-35b-a3b` |

No certificates are needed: the page is plain HTTP and the cluster's certificate isn't checked. The other settings
have sensible defaults; `.env` explains each one. After a change: `docker compose up -d`.

## Before go-live

Run the accuracy check on sample scans whose right answers are known (we provide them), and go live only when it
reports no field that was wrong while marked as sure:

```bash
docker compose run --rm -v "$PWD/scans:/w" reader python -m reader.evaluate /w/ld_test /w/ref.json
```

## Update

Set `IMAGE_TAG` in `.env` to a newer version from the [package page](https://github.com/grega-rotar/ld-docs/pkgs/container/ld-docs),
then `docker compose pull && docker compose up -d`. To go back, set the previous one. Reviews are kept.

## More

- [Installation and operation](docs/zpiz-namestitev.md) (Slovenian): HTTPS, installing without internet, logs, troubleshooting.
- [Guide for clerks](docs/navodila-za-referente.md) (Slovenian).
- API for other systems: `http://<vm>:8080/docs`.
- [Notes for developers](docs/maintainers.md).
