# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.title="laptop-monitor" \
      org.opencontainers.image.version="0.2.0"

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAPTOP_MONITOR_INSTANCE=vps-prod

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY VERSION .
COPY *.py ./
COPY parsers/ ./parsers/
COPY stores/ ./stores/
COPY scripts/backup_db.py ./scripts/backup_db.py
COPY scripts/__init__.py ./scripts/__init__.py

RUN mkdir -p /app/data /app/logs \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "run_pipeline.py", "--version"]
