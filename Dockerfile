# Debian-based image; the primary deployment target is systemd, this is for
# moving the bot between machines without redoing setup.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# tzdata for zoneinfo; build tools are dropped in the final layer
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config
COPY scripts ./scripts

# Non-root user; only /app/data and /app/logs are writable
RUN useradd -r -u 10001 -m news \
    && mkdir -p /app/data /app/logs \
    && chown -R news:news /app
USER news
VOLUME ["/app/data", "/app/logs"]

HEALTHCHECK --interval=5m --timeout=20s --start-period=90s --retries=3 \
    CMD ["python", "-c", "from app.config import get_config; from app.database.database import get_engine; get_engine(); print('ok')"]

ENTRYPOINT ["python", "-m", "app.main"]
CMD []
