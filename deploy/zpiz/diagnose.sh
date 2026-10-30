#!/usr/bin/env bash
# Checks ld-docs on this VM step by step: .env, containers, api -> reader, reader -> vision model, /health.
# Run it in the directory with compose.yml and .env, as the user that started the containers:
#   bash diagnose.sh
# Works with Docker and Podman. Prints no keys, so the output can be sent when asking for help.
set -u
cd "$(dirname "$0")"

FAILED=0
step() { printf '\n== %s\n' "$*"; }
ok()   { printf '  OK    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAILED=1; }
info() { printf '        %s\n' "$*"; }
# One value from .env, without sourcing it (quotes around the value removed).
val()  { sed -n "s/^$1=//p" .env 2>/dev/null | tail -1 | sed "s/^[\"']//; s/[\"']\$//"; }

step "1. .env"
if [ ! -f .env ]; then
  bad ".env not found in $(pwd); copy .env.example to .env and fill it in"
else
  ok ".env found ($(stat -c %a .env), should be 600)"
  for v in API_KEYS READER_KEY VLM_URL VLM_KEY VLM_MODEL; do
    x=$(val "$v")
    if [ -z "$x" ]; then bad "$v is empty"
    elif [[ "$x" == *zamenjaj* ]]; then bad "$v still has the example value (zamenjaj…)"
    else ok "$v is set"; fi
  done
  [[ "$(val VLM_URL)" == */v1 ]] || bad "VLM_URL should end in /v1 (it is $(val VLM_URL))"
  vk=$(val VLM_KEY)
  [ -n "$vk" ] && [ "$(val READER_KEY)" = "$vk" ] && bad "READER_KEY is the same as VLM_KEY; make its own: openssl rand -hex 32"
  [ -n "$vk" ] && [[ ",$(val API_KEYS)," == *",$vk,"* ]] && bad "API_KEYS contains VLM_KEY; whoever signs in has the model key. Make own: openssl rand -hex 32"
fi

step "2. Container engine and containers"
ENGINE=""
for e in docker podman; do
  command -v "$e" >/dev/null || continue
  if "$e" ps -a --format '{{.Names}}' 2>/dev/null | grep -q 'ld-docs[-_]reader'; then ENGINE=$e; break; fi
done
if [ -z "$ENGINE" ]; then
  bad "no ld-docs containers found with docker or podman"
  info "Started? Run 'docker compose up -d' (or 'podman compose up -d') here."
  info "Podman containers are per user: run this script as the user that started them (with or without sudo, the same way)."
  printf '\nSome checks failed. Fix the first FAIL from the top.\n'
  exit 1
fi
ok "engine: $ENGINE ($("$ENGINE" --version | head -1))"
if [ "$ENGINE" = podman ]; then
  backend=$(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null)
  if [ "$backend" = netavark ]; then ok "podman network backend: netavark (containers find each other by name)"
  else bad "podman network backend: ${backend:-unknown}; with cni the api cannot find 'reader' by name (docs/zpiz-namestitev.md)"; fi
fi

API=$("$ENGINE" ps -a --format '{{.Names}}' | grep -m1 'ld-docs[-_]api')
READER=$("$ENGINE" ps -a --format '{{.Names}}' | grep -m1 'ld-docs[-_]reader')
for c in "$API" "$READER"; do
  [ -n "$c" ] || { bad "a container is missing (api: '${API}', reader: '${READER}')"; continue; }
  state=$("$ENGINE" inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
  restarts=$("$ENGINE" inspect -f '{{.RestartCount}}' "$c" 2>/dev/null)
  if [ "$state" = running ]; then
    ok "$c: running (restarts: ${restarts:-?})"
  else
    bad "$c: ${state:-unknown} (restarts: ${restarts:-?}); its last log lines:"
    "$ENGINE" logs --tail 30 "$c" 2>&1 | sed 's/^/        | /'
  fi
done
running() { [ -n "$1" ] && [ "$("$ENGINE" inspect -f '{{.State.Status}}' "$1" 2>/dev/null)" = running ]; }

step "3. api -> reader"
if running "$API"; then
  "$ENGINE" exec -i "$API" python3 - <<'PY' || FAILED=1
import json, os, socket, urllib.parse, urllib.request
url = os.environ.get("READER_URL", "")
print(f"  OK    READER_URL={url}" if url else "  FAIL  READER_URL is not set in the api container")
host = urllib.parse.urlsplit(url).hostname or "reader"
try:
    print(f"  OK    name '{host}' -> {socket.gethostbyname(host)}")
except Exception as e:
    print(f"  FAIL  name '{host}' does not resolve ({e}): the reader is not running, or the network has no DNS; the current compose.yml needs none")
    raise SystemExit(1)
try:
    with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=15) as r:
        print(f"  OK    reader answers: {json.load(r)}")
except Exception as e:
    print(f"  FAIL  reader does not answer at {url}/health: {e}")
    raise SystemExit(1)
PY
else
  bad "skipped: the api container is not running"
fi

step "4. VM -> vision model (from the VM itself)"
vlm_url=$(val VLM_URL)
host=$(printf '%s' "$vlm_url" | sed -E 's#^[a-z]+://##; s#[/:].*##')
if [ -n "$host" ]; then
  if ip=$(getent hosts "$host" | awk '{print $1; exit}') && [ -n "$ip" ]; then ok "name $host -> $ip"
  else bad "name $host does not resolve on the VM (VM DNS; ZPIZ IT)"; fi
  if timeout 5 bash -c "</dev/tcp/$host/443" 2>/dev/null; then ok "port 443 on $host is open"
  else bad "cannot connect to $host:443 (firewall or proxy between the VM and the cluster)"; fi
fi

step "5. reader -> vision model (from inside the reader container, with its own settings)"
if running "$READER"; then
  "$ENGINE" exec -i "$READER" python - <<'PY' || FAILED=1
import json, os, socket, ssl, time, urllib.error, urllib.parse, urllib.request
url = os.environ.get("VLM_URL", "").rstrip("/")
key = os.environ.get("VLM_KEY", "")
model = os.environ.get("VLM_MODEL", "")
parts = urllib.parse.urlsplit(url)
host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
ctx = ssl._create_unverified_context() if os.environ.get("VLM_VERIFY_TLS", "1") == "0" else None
print(f"  OK    VLM_URL={url}  VLM_MODEL={model}  VLM_VERIFY_TLS={os.environ.get('VLM_VERIFY_TLS', '1')}")

def fail(msg):
    print(f"  FAIL  {msg}")
    raise SystemExit(1)

def call(path, body=None, timeout=15):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        hint = {401: "VLM_KEY is wrong or expired", 403: "VLM_KEY has no access to this model",
                404: "wrong VLM_URL or VLM_MODEL"}.get(e.code, "")
        fail(f"{path}: HTTP {e.code} {hint}: {e.read()[:200]!r}")
    except ssl.SSLError as e:
        fail(f"{path}: TLS error ({e}); set VLM_VERIFY_TLS=0 in .env")
    except Exception as e:
        fail(f"{path}: {e}")

try:
    print(f"  OK    name {host} -> {socket.gethostbyname(host)}")
except Exception as e:
    fail(f"name {host} does not resolve inside the container ({e}); the VM may resolve it but the container's DNS not")
try:
    socket.create_connection((host, port), 5).close()
    print(f"  OK    port {port} on {host} is open")
except Exception as e:
    fail(f"cannot connect to {host}:{port} from the container ({e})")

ids = [m.get("id") for m in call("/models").get("data", [])]
if model in ids:
    print(f"  OK    key works, model {model} is listed")
else:
    fail(f"key works, but model {model} is not listed; listed: {ids}")

t = time.time()
answer = call("/chat/completions", {
    "model": model, "temperature": 0, "max_tokens": 8, "chat_template_kwargs": {"enable_thinking": False},
    "messages": [{"role": "user", "content": "Answer with only the word OK."}]}, timeout=120)
text = (answer.get("choices") or [{}])[0].get("message", {}).get("content")
print(f"  OK    model answers in {time.time() - t:.1f} s: {text!r}")

# The reader sends the model crops of handwriting, not text: the same kind of call, with an image of "SEK".
import base64, io
from PIL import Image, ImageDraw
im = Image.new("RGB", (200, 70), "white")
ImageDraw.Draw(im).text((20, 15), "SEK", fill="black", font_size=36)
buf = io.BytesIO()
im.save(buf, "PNG")
t = time.time()
answer = call("/chat/completions", {
    "model": model, "temperature": 0, "max_tokens": 16, "chat_template_kwargs": {"enable_thinking": False},
    "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}},
        {"type": "text", "text": "Transcribe the text. Answer with only the text."}]}]},
    timeout=int(os.environ.get("VLM_TIMEOUT", "120")))
text = (answer.get("choices") or [{}])[0].get("message", {}).get("content") or ""
if "SEK" in text.upper():
    print(f"  OK    model reads an image in {time.time() - t:.1f} s: {text!r}")
else:
    fail(f"model got an image of 'SEK' and answered {text!r} (does this route pass images to a vision model?)")
PY
else
  bad "skipped: the reader container is not running"
fi

step "6. /health (the whole chain, as the page sees it)"
# The port the api is really published on (compose.yml may have been changed), else HTTP_PORT from .env.
port=$("$ENGINE" port "$API" 8000 2>/dev/null | head -1 | sed 's/.*://')
[ -n "$port" ] || { port=$(val HTTP_PORT); port=${port:-8080}; }
health_url="http://127.0.0.1:$port/health"
if command -v curl >/dev/null; then health=$(curl -s -m 15 "$health_url")
else health=$(python3 -c "import urllib.request; print(urllib.request.urlopen('$health_url', timeout=15).read().decode())" 2>&1); fi
if [ -z "$health" ]; then bad "no answer at $health_url (api not running, or HTTP_PORT/HTTP_BIND differ)"
elif printf '%s' "$health" | grep -Eq '"local": ?\{"ok": ?true'; then ok "$health"
else bad "$health"; info "local.ok must be true: the api reaches the reader and the reader reaches the model"; fi

if [ "$FAILED" = 0 ]; then printf '\nAll checks passed.\n'
else printf '\nSome checks failed. Fix the first FAIL from the top.\n'; exit 1; fi
