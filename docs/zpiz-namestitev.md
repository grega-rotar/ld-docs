# ld-docs pri ZPIZ: namestitev in delovanje

Navodila za IT ZPIZ. Storitev bere obrazce »Izjava o višini tuje pokojnine« (LD) in vrne JSON za ZPIZ
(`LDDokumentP1` + `LDDokumentP2`). Referenti jo uporabljajo v brskalniku (navodila zanje:
[navodila-za-referente.md](navodila-za-referente.md)), drugi sistemi pa prek API-ja (`/docs` na strežniku,
opis v [README.md](../README.md)).

Datoteke za namestitev so v [`deploy/zpiz/`](../deploy/zpiz/): `compose.yml` in `.env.example`.

## Arhitektura

Dva vsebnika na enem Linux VM, ki ju zažene `docker compose`:

```
 referenti (brskalnik)                        VM pri ZPIZ                         OpenShift AI (ZPIZ)
 ┌──────────────┐  HTTPS   ┌──────────┐ HTTP  ┌─────────────────────────────┐
 │ brskalnik    │ ───────▶ │ reverse  │ ────▶ │ api     :8080 (-> 8000)     │
 └──────────────┘          │ proxy    │       │   │ http://reader:8100      │
                           │ (TLS)    │       │   ▼                         │  HTTPS  ┌───────────────────┐
                           └──────────┘       │ reader  (brez vrat navzven) │ ──────▶ │ vLLM, Qwen3.6     │
                                              │ volume ld_data (/data)      │         │ .../v1            │
                                              └─────────────────────────────┘         └───────────────────┘
```

- **api** (`ghcr.io/grega-rotar/ld-docs`): spletna stran za referente in API `/v1`. Razreže naložene strani
  na dokumente po QR kodah, pošlje vsak dokument bralniku, hrani rezultate in popravke referentov.
- **reader** (`ghcr.io/grega-rotar/ld-reader`): prebere dokument na CPU (OpenCV, Tesseract, majhna mreža za
  števke). Samo za lastnoročno napisana polja (valuta, država, znesek) pošlje majhen izrez polja modelu
  Qwen3.6 na OpenShift AI. Cele strani nikoli ne zapustijo VM.
- Iz VM gre ven samo promet do `VLM_URL`. Claude in druge zunanje storitve se v tej različici ne uporabljajo
  (`READERS=local`).

## Zahteve

| | |
|---|---|
| Sistem | Linux VM (npr. RHEL 9, Ubuntu 24.04), x86_64 |
| Programi | Docker Engine 24+ z vtičnikom compose (`docker compose version`) |
| CPU, RAM | začetek: 2–4 vCPU, 4 GB RAM (ocena). Bralnik dela na CPU, okrog 1–2 s na stran plus čas modela. GPU ni potreben. |
| Disk | okrog 20 GB: sliki skupaj nekaj GB, rezultati paketov so majhni (JSON) |
| Omrežje ven | HTTPS (443) do gostitelja v `VLM_URL`; za prenos slik še `ghcr.io` (ali namestitev brez povezave, spodaj) |
| Omrežje noter | `HTTP_PORT` (8080) samo z reverse proxyja ali iz omrežja referentov |

Hitrost je ocena, ker Qwen3.6 na gruči pri nas še ni bil izmerjen: en dokument (2 strani) nekaj sekund do
okrog 15 s, paket 51 dokumentov nekaj minut. Več hkratnih dokumentov: `READER_PARALLEL` (okrog 1 na vCPU) in
`LOCAL_PARALLEL`.

## Omrežje in TLS

API sam govori samo HTTP. Ključi za dostop gredo v glavi `Authorization`, zato naj bo pred njim reverse proxy
ZPIZ s TLS. Če proxy teče na istem VM, nastavite `HTTP_BIND=127.0.0.1`, da vrata 8080 niso dosegljiva od
drugod (Docker objavljena vrata odpre mimo `firewalld`/`ufw`).

Primer za nginx (nalaganje paketa je lahko nekaj sto MB, en dokument v `/v1/extract` se bere do nekaj minut):

```nginx
server {
    listen 443 ssl;
    server_name ld-docs.zpiz.si;
    ssl_certificate     /etc/nginx/tls/ld-docs.crt;
    ssl_certificate_key /etc/nginx/tls/ld-docs.key;

    client_max_body_size 1g;
    proxy_read_timeout   300s;
    proxy_send_timeout   300s;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Namestitev

```bash
sudo mkdir -p /opt/ld-docs && cd /opt/ld-docs
# compose.yml in .env.example iz repozitorija (deploy/zpiz/)
curl -fsSLO https://raw.githubusercontent.com/grega-rotar/ld-docs/main/deploy/zpiz/compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/grega-rotar/ld-docs/main/deploy/zpiz/.env.example
chmod 600 .env
# vpišite prave vrednosti (spodaj "Nastavitve"); IMAGE_TAG nastavite na izbrani git sha
vi .env
docker compose config --quiet      # preveri .env: manjkajoča obvezna spremenljivka se izpiše
docker compose pull
docker compose up -d
docker compose ps                  # oba vsebnika "healthy" po okrog 30 s
curl -s http://127.0.0.1:8080/health
```

Nato odprite `http://<vm>:8080` (ali naslov za proxyjem), se prijavite z enim od `API_KEYS` in naložite
nekaj vzorčnih skenov.

## Nastavitve (`.env`)

Ena datoteka `.env` poleg `compose.yml`. Compose da vsakemu vsebniku samo spremenljivke, ki jih potrebuje
(API ne vidi `VLM_KEY`, bralnik ne vidi `API_KEYS`).

| Spremenljivka | Privzeto | Pomen |
|---|---|---|
| `IMAGE_TAG` | `latest` | različica obeh slik, git sha (npr. `633879a…`). Za produkcijo vpišite sha, ne `latest`. |
| `HTTP_BIND` | `0.0.0.0` | naslov na VM, kjer posluša API; `127.0.0.1`, če je proxy na istem VM |
| `HTTP_PORT` | `8080` | vrata na VM |
| `API_KEYS` | (obvezno) | ključi za dostop, ločeni z vejico, eden na odjemalca (referenti, drug sistem …) |
| `READER_KEY` | (obvezno) | ključ med API in bralnikom; compose ga da obema (bralniku kot `READER_KEYS`) |
| `VLM_API` | `openai` | `openai` = strežnik z API-jem, združljivim z OpenAI (vLLM, OpenShift AI MaaS) |
| `VLM_URL` | (obvezno) | osnovni URL modela, konča se z `/v1`, npr. `https://<model-route>.apps.<cluster>.<domain>/v1`. Tudi oblika MaaS (`…/<projekt>/<model>/v1`) je samo drug `VLM_URL`. |
| `VLM_KEY` | (obvezno) | žeton service accounta ali ključ MaaS; pošlje se kot `Authorization: Bearer …` |
| `VLM_MODEL` | `redhataiqwen36-35b-a3b` | ime modela, kot ga vrne `GET {VLM_URL}/models` |
| `VLM_VERIFY_TLS` | `0` | `0` = certifikat gruče se ne preverja, certifikati na VM niso potrebni (ob zagonu se izpiše opozorilo); `1` = preverja se s sistemskimi CA |
| `VLM_TIMEOUT` | `120` | sekunde čakanja na en odgovor modela |
| `JOB_TTL_DAYS` | `7` | po koliko dneh se rezultati paketa izbrišejo |
| `LOCAL_PARALLEL` | `3` | koliko dokumentov paketa API hkrati pošlje bralniku |
| `READER_PARALLEL` | `2` | koliko dokumentov bralnik bere hkrati (okrog 1 na vCPU) |

`READERS=local` in `READER_URL=http://reader:8100` sta zapisana v `compose.yml` in se ne spreminjata.

### Ključi

```bash
openssl rand -hex 32
```

En ključ za vsakega odjemalca v `API_KEYS` (npr. en za referente, en za vsak sistem, ki kliče API), tako da
lahko enega zamenjate brez drugih. Za `READER_KEY` ločen ključ. Po spremembi `.env`: `docker compose up -d`
(ponovno ustvari samo spremenjene vsebnike).

## Preverjanje modela z VM

Pred prvim zagonom preverite, da VM doseže model. Vrednosti preberite iz `.env`, da žeton ni v zgodovini ukazov:

```bash
cd /opt/ld-docs && set -a && . ./.env && set +a

# 1. seznam modelov: med "id" mora biti VLM_MODEL
curl -sSk -H "Authorization: Bearer $VLM_KEY" "$VLM_URL/models"

# 2. besedilni klic
curl -sSk -H "Authorization: Bearer $VLM_KEY" -H "Content-Type: application/json" "$VLM_URL/chat/completions" \
  -d '{"model": "'"$VLM_MODEL"'", "temperature": 0, "max_tokens": 8,
       "chat_template_kwargs": {"enable_thinking": false},
       "messages": [{"role": "user", "content": "Answer with only the word OK."}]}'
```

3. Klic s sliko, tak kot ga pošilja bralnik (izrez z napisom `SEK`, slika se naredi v bralnikovi sliki):

```bash
IMG=$(docker run --rm ghcr.io/grega-rotar/ld-reader:$IMAGE_TAG python -c "
import base64, io
from PIL import Image, ImageDraw
im = Image.new('RGB', (200, 70), 'white')
ImageDraw.Draw(im).text((20, 15), 'SEK', fill='black', font_size=36)
b = io.BytesIO(); im.save(b, 'PNG'); print(base64.b64encode(b.getvalue()).decode())")
printf '{"model": "%s", "temperature": 0, "max_tokens": 16, "chat_template_kwargs": {"enable_thinking": false},
 "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,%s"}},
 {"type": "text", "text": "Transcribe the text. Answer with only the text."}]}]}' "$VLM_MODEL" "$IMG" > /tmp/probe.json
curl -sSk -H "Authorization: Bearer $VLM_KEY" -H "Content-Type: application/json" \
  "$VLM_URL/chat/completions" -d @/tmp/probe.json
rm /tmp/probe.json
```

V odgovoru mora biti `"content": "SEK"` (v `choices[0].message`). Napake: `401` žeton ni veljaven ali je potekel,
`404` napačen `VLM_URL` ali `VLM_MODEL`.

### Kdaj poteče žeton

Žeton service accounta je JWT; datum poteka je v polju `exp`:

```bash
p=$(echo "$VLM_KEY" | cut -d. -f2 | tr '_-' '/+'); while [ $(( ${#p} % 4 )) -ne 0 ]; do p="$p="; done
exp=$(echo "$p" | base64 -d | grep -o '"exp":[0-9]*' | cut -d: -f2); date -d "@$exp"
```

Žeton iz e-pošte o vLLM poteče 10. 9. 2027. Pred tem pripravite novega, ga vpišite v `.env` in
`docker compose up -d`. Ključ MaaS ni nujno JWT; njegov rok poveste skrbniki OpenShift AI.

## Zdravje

```bash
curl -s http://127.0.0.1:8080/health
# {"ok": true, "reader": "local", "readers": ["local"], "local": {"ok": true, "model": "redhataiqwen36-35b-a3b"}}
```

- `ok`: API teče.
- `readers`: mora biti `["local"]`.
- `local.ok`: bralnik je dosegljiv **in** model je na seznamu `GET {VLM_URL}/models`. `false` pomeni, da
  bralnik ne teče ali ne doseže modela; referenti v glavi strani vidijo »Bralnik ne deluje«.
  Stanje se osveži vsakih 15 s.
- `docker compose ps` pokaže healthcheck obeh vsebnikov. Bralnik je "healthy", ko teče, tudi če modela ne
  doseže; to pove `local.ok`.

`/health` ne potrebuje ključa in je primeren za nadzor (npr. Zabbix: `local.ok == true`).

## Dnevniki

```bash
docker compose logs -f --tail 200          # oba
docker compose logs reader --since 1h      # samo bralnik
```

Dnevniki so v Dockerjevem formatu json-file in se vrtijo: največ 5 × 10 MB na vsebnik.

## Posodobitev in vrnitev

Sliki sta označeni z git sha (in `latest` za zadnjo). Obe imata vedno isti sha, zato je v `.env` ena
spremenljivka `IMAGE_TAG`.

```bash
cd /opt/ld-docs
# v .env: IMAGE_TAG=<nov sha>; prejšnjega si zapišite
docker compose pull && docker compose up -d
curl -s http://127.0.0.1:8080/health
```

Vrnitev: v `.env` vpišite prejšnji `IMAGE_TAG` in ponovite ista ukaza. Razpoložljive oznake so na
`https://github.com/grega-rotar/ld-docs/pkgs/container/ld-docs`; lokalne pokaže
`docker image ls ghcr.io/grega-rotar/ld-docs`.

Posodabljajte, ko se paket ne bere: paket, ki se bere med ponovnim zagonom, dobi stanje `interrupted` in ga
je treba naložiti znova. Že prebrani paketi in popravki ostanejo (volume `ld_data`).

## Namestitev brez povezave

Če VM ne doseže `ghcr.io`, sliki prenesite na računalniku z internetom:

```bash
TAG=<git sha>
docker pull ghcr.io/grega-rotar/ld-docs:$TAG
docker pull ghcr.io/grega-rotar/ld-reader:$TAG
docker save ghcr.io/grega-rotar/ld-docs:$TAG ghcr.io/grega-rotar/ld-reader:$TAG | gzip > ld-docs-$TAG.tar.gz
```

Datoteko prenesite na VM in:

```bash
docker load -i ld-docs-$TAG.tar.gz
# v .env: IMAGE_TAG=<isti sha>
docker compose up -d            # brez "pull": slike so že lokalno
```

## Podatki in osebni podatki

- Rezultati paketov (prebrana polja, popravki referentov, imena datotek) so datoteke JSON v volumnu
  `ld_data` (`/data/jobs` v vsebniku `api`, na VM `docker volume inspect ld-docs_ld_data`).
- Po `JOB_TTL_DAYS` dneh (privzeto 7) se izbrišejo samodejno.
- Skeni se ne hranijo: po razrezu na dokumente in branju se zavržejo.
- Varnostna kopija ni potrebna. Izid dela je izvoženi JSON/CSV, ki ga referent shrani v sistem ZPIZ.
- Vse rezultate takoj izbrišete z `docker compose down -v` (izbriše tudi volume).
- Modelu na OpenShift AI gredo samo izrezi posameznih lastnoročnih polj (valuta, država, znesek).

## Varnost

- En ključ v `API_KEYS` na odjemalca; ključ, ki ga ne potrebujete več ali je ušel, odstranite iz `.env` in
  `docker compose up -d`. Ključe menjajte redno (npr. letno) in ob odhodu ljudi, ki so jih poznali.
- `.env` naj ima način `600` in lastnika, ki upravlja storitev. V njem so `API_KEYS`, `READER_KEY` in
  `VLM_KEY`.
- `.env` nikoli ne dajte v git ali v zahtevke za podporo. Pri pošiljanju dnevnikov preverite,
  da v njih ni ključev.
- Pred API mora biti TLS (reverse proxy); vrata 8080 naj bodo dosegljiva samo s proxyja.
- Bralnik nima objavljenih vrat; doseže ga samo `api` v omrežju compose, z `READER_KEY`.

## Preverjanje natančnosti pred začetkom dela

Bralnik je bil uglašen z modelom Qwen3-VL 8B. Qwen3.6 na gruči pri nas ni bil preizkušen, zato pred
začetkom dela preberite 51 vzorčnih dokumentov ZPIZ in jih primerjajte z referenco:

```bash
cd /opt/ld-docs
mkdir -p results && chmod 777 results    # bralnik piše kot uid 1000
# ld_test/ = vzorčni skeni, results/ref.json = referenčni rezultati (dobite jih od nas skupaj z navodili)
docker compose run --rm --no-deps -v "$PWD/ld_test:/scans:ro" -v "$PWD/results:/results" reader \
  python -m reader.evaluate /scans /results/ref.json --out /results/local.json
```

Izpiše vsako razliko in, kar je najpomembneje, polja, ki so **napačna, a z zanesljivostjo ≥ 0,9** (izhodna
koda 1, če je tak vsaj eno). Teh mora biti 0: polja pod 0,9 so referentom označena za preverbo, polja nad
tem pa ne. Če niso 0, nam pošljite izpis brez osebnih podatkov (samo imena polj in datotek) in počakajte s
produkcijo.

Za nove vrste skenov je referenca lahko izvoz JSON paketa, v katerem je referent preveril **vsa** polja, ne
samo označenih. Skeni in rezultati vsebujejo osebne podatke: po preverjanju izbrišite `ld_test/` in
`results/`.

## Težave

| Kaj vidite | Vzrok | Kaj storiti |
|---|---|---|
| Glava strani: »Bralnik ne deluje«, `/health` `local.ok: false` | bralnik ne teče ali ne doseže modela | `docker compose ps`, `docker compose logs reader`; preverite model z VM (zgoraj). Dokumenti v vrsti se preberejo, ko bralnik spet deluje (»Poskusi znova«). |
| Referent: »Ključ ni pravilen« (`401` z API-ja) | ključa ni v `API_KEYS` | dodajte ga v `.env`, `docker compose up -d` |
| `401` v dnevniku bralnika pri klicu modela | `VLM_KEY` napačen ali potekel | nov žeton, `.env`, `docker compose up -d`; rok preverite (zgoraj) |
| `503` ali dokumenti s stanjem napake »vision model unavailable« | model na gruči ne odgovarja (5xx, ni zagnan, preobremenjen) | stanje modela v OpenShift AI; dokumente naložite znova, ko deluje |
| `503` na vse klice API-ja | `API_KEYS` prazen | vpišite ključe |
| Vse je delovalo, nato `401` od modela | žeton je potekel | glejte »Kdaj poteče žeton« |
| Branje je počasno | malo CPU, model zaseden, premalo hkratnih dokumentov | `docker stats`; več vCPU in `READER_PARALLEL`/`LOCAL_PARALLEL`; čas odgovora modela preverite s klicem s sliko (zgoraj) |
| `docker compose` javi `required variable … is missing a value` | obvezna spremenljivka manjka v `.env` | vpišite jo (`API_KEYS`, `READER_KEY`, `VLM_URL`, `VLM_KEY`) |
| Nalaganje velikega paketa se prekine (`413`) | omejitev reverse proxyja | `client_max_body_size` v nginx |
