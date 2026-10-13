# Platform load test

Closed-loop load test of `platform/` `POST /query`, stdlib-only client (`run.py`), keyless default
config (hash embedder, no `ANTHROPIC_API_KEY`, extractive answers, no paid calls). Results:
`evals/results/2026-10-12-load-test/`.

What it measures: req/s, p50/p95/p99 client latency, error rate (non-200 or `source: unavailable`),
the server's own `retrieve_ms` / `total_ms` (joined by `X-Request-ID` from the per-request log line),
and permission leaks (every returned result checked by `lib.Oracle`, which re-derives document AND
section AND paragraph ACLs from the corpus files; unknown text counts as a leak).

Each VU is one of 10 personas (round-robin), one keep-alive connection, no think time.
`--mode miss` appends a unique token to each query so the per-process answer cache never hits;
`--mode hit` reuses 10 fixed queries (cache warm); `--mode health` hits `/healthz` to find the
client's own ceiling.

## Reproduce

```bash
# 1. platform venv in a gitignored path (never format platform/ with ruff)
uv venv -p 3.12 .venv-load && uv pip install -p .venv-load/bin/python -r platform/requirements-dev.txt
export SCRATCH=$(mktemp -d)                      # holds the DB, Qdrant dir, keys, tokens
export DATABASE_URL=sqlite:///$SCRATCH/rag.db QDRANT_PATH=$SCRATCH/qdrant \
       JWT_PRIVATE_KEY_PATH=$SCRATCH/config/idp_private.pem JWT_PUBLIC_KEY_PATH=$SCRATCH/config/idp_public.pem
unset ANTHROPIC_API_KEY
PY=$PWD/.venv-load/bin/python

# 2. keys, seed corpus (12 chunks), tokens (DEMO_MODE is NOT needed: tokens are minted by the script)
cd platform && $PY scripts/gen_keys.py && $PY scripts/ingest.py && cd ..
(cd platform && PYTHONPATH=. $PY ../evals/load/mint_tokens.py $SCRATCH/tokens.json)

# 3. server (default config; SYNC_INTERVAL_S stays at its default of 10)
(cd platform && $PY -m uvicorn app.main:app --port 8091 > $SCRATCH/s1.log 2>&1 &)

# 4. load: 1,5,10,25,50 VUs, 5 s warm-up (discarded) + 60 s measured each
python3 evals/load/run.py run --urls http://127.0.0.1:8091 --tokens $SCRATCH/tokens.json \
    --label 1worker-unique-queries --mode miss --server-logs $SCRATCH/s1.log --out $SCRATCH/runs

# 5. two workers: Qdrant local mode locks its directory, so a second process needs its own copy.
#    Same SQLite file (shared permission lock), separate Qdrant copy, client round-robins both ports.
cp -R $SCRATCH/qdrant $SCRATCH/qdrant_b
(cd platform && QDRANT_PATH=$SCRATCH/qdrant_b $PY -m uvicorn app.main:app --port 8092 > $SCRATCH/s2.log 2>&1 &)
python3 evals/load/run.py run --urls http://127.0.0.1:8091,http://127.0.0.1:8092 \
    --tokens $SCRATCH/tokens.json --label 2workers-unique-queries --mode miss \
    --server-logs $SCRATCH/s1.log,$SCRATCH/s2.log --out $SCRATCH/runs

# 6. render table.md + report.json
python3 evals/load/run.py render $SCRATCH/runs --out evals/results/2026-10-12-load-test
```

Stage timings (diagnosis): `cd platform && $PY ../evals/load/profile_server.py 8093` runs the same
app with wall-clock timers around the permission transaction, embedding, vector search and audit
write (no platform code changed); `curl localhost:8093/__timings?reset=1` after a run.

Caveats: 12-chunk synthetic corpus, one laptop, client and server share the machine, single run
per cell. Orders of magnitude, not capacity planning.
