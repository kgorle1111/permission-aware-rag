FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DATABASE_URL=sqlite:////data/rag.db \
    QDRANT_PATH=/data/qdrant_data \
    JWT_PRIVATE_KEY_PATH=/data/idp_private.pem \
    JWT_PUBLIC_KEY_PATH=/data/idp_public.pem \
    PERMISSIONS_SOURCE=/app/corpus/permissions.jsonl \
    PERMISSIONS_BACKEND=jsonl \
    DEMO_MODE=0 \
    SYNC_INTERVAL_S=10 \
    JWKS_URL=""

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY corpus ./corpus
COPY static ./static

RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data \
    && chown app:app /data

VOLUME ["/data"]
EXPOSE 8090
USER app

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/readyz', timeout=2)"]

# Only DEMO_MODE=1 mints a local keypair; production must bring JWKS_URL or a mounted public key.
# validate_identity_config (also run by the app's lifespan) refuses a bad identity setup before ingest
# writes to /data. --limit-concurrency 64 bounds memory: past 64 in-flight connections uvicorn
# answers 503 instead of queueing.
CMD ["sh", "-c", "set -eu; umask 077; if [ \"$DEMO_MODE\" = 1 ] && [ -z \"$JWKS_URL\" ] && [ ! -e \"$JWT_PRIVATE_KEY_PATH\" ] && [ ! -e \"$JWT_PUBLIC_KEY_PATH\" ]; then python scripts/gen_keys.py; fi; python -c 'from app.identity import validate_identity_config as v; v()'; if [ ! -f /data/.corpus-ingested ]; then python scripts/ingest.py; touch /data/.corpus-ingested; fi; exec uvicorn app.main:app --host 0.0.0.0 --port 8090 --limit-concurrency 64"]
