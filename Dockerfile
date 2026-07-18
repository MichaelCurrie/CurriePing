# Multi-arch base (runs on ARM64 t4g instances and x86 alike).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATUS_DB_PATH=/data/status.db

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Persisted SQLite history lives here (mounted as a volume in compose).
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# Single process => exactly one background checker thread.
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8080", "app.main:app"]
