FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/huggingface \
    HF_HUB_CACHE=/models/huggingface/hub \
    HF_XET_CACHE=/models/huggingface/xet \
    XDG_CACHE_HOME=/models/cache \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        ffmpeg \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv

WORKDIR /app

COPY requirements.lock .
RUN pip install --upgrade pip \
    && pip install --requirement requirements.lock

COPY requirements-tools.lock .
RUN pip install --requirement requirements-tools.lock

COPY app ./app

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data /models \
    && chown -R app:app /data /models /app

USER app

EXPOSE 8000
VOLUME ["/data", "/models"]

HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
