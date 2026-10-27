# syntax=docker/dockerfile:1.7
# The local reader (README "Local reader"): OpenCV + Tesseract + a small digit CNN on the CPU,
# Qwen3-VL through the ollama container on the GPU, or an OpenAI-compatible server with VLM_API=openai
# (reader/vlm.py). No API keys or scans in here.
FROM python:3.12-slim
# Tesseract: English for the printed codes and numbers, the script models (Latin, Cyrillic, Greek) for
# the yes/no words in any language of those alphabets.
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-srp tesseract-ocr-mkd \
      tesseract-ocr-script-latn tesseract-ocr-script-cyrl tesseract-ocr-script-grek \
      libzbar0 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
COPY reader/requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt
RUN useradd -u 1000 -m reader
WORKDIR /srv
COPY --chown=reader:reader app ./app
COPY --chown=reader:reader reader ./reader
USER reader
ENV PYTHONUNBUFFERED=1 OLLAMA_URL=http://ollama:11434
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health')"]
CMD ["python", "-m", "uvicorn", "reader.server:app", "--host", "0.0.0.0", "--port", "8100"]
