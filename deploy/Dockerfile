# syntax=docker/dockerfile:1
# bookworm: Playwright --with-deps Chromium matches Debian 12 packages
# (python:3.12-slim floated to trixie and breaks font deps).
FROM python:3.12-slim-bookworm

ARG APP_VERSION=unknown
LABEL org.opencontainers.image.title="laptop-monitor" \
      org.opencontainers.image.version="${APP_VERSION}"

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAPTOP_MONITOR_INSTANCE=vps-prod \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

# Build context = extracted release bundle (+ host-only .env/data/logs/backups).
# Root .dockerignore is a whitelist matching scripts/release_manifest.py
# IMAGE_ALLOW_PATTERNS; never list individual modules here.
COPY . ./

RUN mkdir -p /app/data /app/logs \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "run_pipeline.py", "--version"]
