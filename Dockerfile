# syntax=docker/dockerfile:1.7
# The API image: FastAPI, zbar for the QR codes, the claude CLI and claude-pool (README.md).
# It is public on GHCR, so no secrets go in here: API keys, claude-pool accounts and certs come in on the server.

FROM node:24-bookworm-slim
ARG CLAUDE_CODE_VERSION=2.1.304
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip libzbar0 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && npm cache clean --force
# claude-pool v0.16.1, pinned by hash: https://github.com/grega-rotar/claude-pool/releases/tag/v0.16.1
ADD --chmod=755 --checksum=sha256:e96ee87a1dc6ae953639062ec10a8545e33e64aef76d01c3d4aa004913567300 \
    https://github.com/grega-rotar/claude-pool/releases/download/v0.16.1/claude_pool.py /usr/local/bin/claude-pool

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Non-root user `app` (uid 1000) in place of the image's `node` user, because Claude runs in here.
RUN userdel -r node && useradd -u 1000 -m -d /home/app -s /bin/bash app \
 && mkdir -p /data/jobs /home/app/.claude /home/app/.config/claude-pool \
 && chown -R app:app /data /home/app

WORKDIR /srv
COPY --chown=app:app app ./app
USER app
ENV JOBS_DIR=/data/jobs \
    CLAUDE_BIN=claude-pool \
    CLAUDE_POOL_DIR=/home/app/.config/claude-pool \
    CLAUDE_POOL_STATE=/data/claude-pool-state.json \
    DISABLE_AUTOUPDATER=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
