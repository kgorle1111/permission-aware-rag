# Permission-Aware RAG

A small reference application for JWT-authenticated, permission-filtered retrieval. It combines a FastAPI API, Qdrant vector search, SQL-backed permission state and audit records, local embeddings by default, and either a synthetic JSONL feed or an optional Google Drive Changes API backend. It is intended for local evaluation and development; it is not a validated production deployment.

## Run locally

Requires Python 3.11 or 3.12.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/gen_keys.py
python scripts/ingest.py
DEMO_MODE=1 uvicorn app.main:app --port 8090
```

The demo UI is at <http://localhost:8090>. In another terminal, `python scripts/demo.py` runs the scripted example against the running API. `DEMO_MODE=1` enables development token minting and must not be used for a real deployment.

For a containerized local demo, use `DEMO_MODE=1 docker compose up --build`. With `DEMO_MODE=1` the container generates a local signing keypair on first start, ingests the corpus, and stores SQL, Qdrant data, and runtime keys in the persistent `rag-data` volume mounted at `/data`. Without `DEMO_MODE=1` (compose defaults it to `0`) the container never mints its own keys: it needs `JWKS_URL` set to your IdP's JWKS endpoint, or an IdP public key mounted at `JWT_PUBLIC_KEY_PATH`, and otherwise exits with `no identity provider configured: set JWKS_URL to your IdP's JWKS endpoint, or DEMO_MODE=1 for a local demo`. `docker compose down` keeps the volume; `docker compose down -v` deletes the data and runtime keys. Back it up and restore it with `scripts/backup.sh` and `scripts/restore.sh`, and back up before upgrading the image: see [docs/BACKUP_RESTORE.md](../docs/BACKUP_RESTORE.md) for coverage, the restore drill and migration rollback. Compose runs the container with a read-only root filesystem; only `/data` (the volume) and `/tmp` (a tmpfs) are writable.

Startup fails loudly on a bad identity setup, whichever way the app is launched (Docker, `uvicorn`, or the test client): an unparsable public key file, a `JWKS_URL` that cannot be fetched within 5 seconds, or `DEMO_MODE=1` combined with `JWKS_URL` stops the process with an error naming the variable to fix. Request bodies over 64 KB get `413` before they are buffered (declared `Content-Length` or chunked), and the container runs uvicorn with `--limit-concurrency 64`, which answers `503` past 64 in-flight connections.

## Test

```sh
pip install -r requirements-dev.txt -r requirements-pdf.txt   # the PDF tests also need the tesseract binary
python -m pytest tests/ -q --cov=app --cov=scripts --cov-branch --cov-fail-under=96
mutmut run --max-children 1 && mutmut export-cicd-stats
python scripts/check_mutations.py mutants/mutmut-cicd-stats.json --mutants-dir mutants \
  --equivalents mutation_equivalents.json --source-root .
```

The mutation check fails on any surviving mutant that isn't a reviewed, hash-pinned
equivalent in `mutation_equivalents.json`. CI runs all three on every push
(`.github/workflows/platform.yml` at the repo root).

`app/pdf_ingest.py` is excluded from the mutation gate (`do_not_mutate` in `pyproject.toml`): it is extraction heuristics (layout, font and OCR thresholds), where most mutants are equivalent boundary changes. Its security property, that every extracted chunk receives the document ACL and redaction, is enforced in `ingest.chunk_document`, which remains mutated, and `pdf_ingest.py` keeps 100% line and branch coverage.

## Updating dependencies

`requirements.txt` holds the version ranges; the Docker image installs `requirements.lock`, which pins exact versions with hashes (`pip install --require-hashes`), so two builds of one commit install the same packages. After editing `requirements.txt`, regenerate the lock from the repo root and commit both files:

```sh
uv pip compile platform/requirements.txt --generate-hashes --python-version 3.12 --python-platform x86_64-manylinux_2_28 -o platform/requirements.lock
```

The lock is resolved for linux/amd64 on Python 3.12, the production image's platform. The base image is pinned by digest to the same `python:3.12-slim` digest as `deploy/aws/Dockerfile`; bump both together.

## Current behavior and boundaries

- Requests authenticate with RS256 JWTs. Retrieval applies the ACL predicate in the Qdrant query and constructs the answer from returned chunks. Access checks, generation, and the success audit write are performed inside the permission-state transaction/locking boundary. If reconciliation is marked pending or retrieval fails, the request returns the empty response.
- Each chunk stores document, section and paragraph ACLs independently. Qdrant applies an AND of three principal-overlap filters before ranking; a caller must satisfy every level. Missing child restrictions are public at that level; explicit empty/malformed ACLs deny. This implements hierarchy, not relationship-based authorization. Legacy indexes require full reingestion before sync can unblock retrieval. Pending upgrades from releases without document journals also require reingestion: startup commits the rebuild barrier before creating recovery tables. Clean pre-journal indexes can upgrade normally. See [upgrade recovery](../docs/SYNC_RECONCILIATION.md#upgrading-an-index-created-before-journals).
- Embedding fingerprint: ingest records backend/model/dimension; a mismatch or a legacy index without one fails closed (empty response, `/readyz` 503) until reingested. `EMBED_MODEL` optionally names the model.
- Optional local models (off by default; see below). Real embeddings, learned-sparse hybrid and a cross-encoder reranker run in-process via ONNX, and every one is ACL-safe by construction: the sparse leg carries no collection-wide statistics, the filter rides on both hybrid branches, and the reranker only receives chunks the engine already filtered.
- PDF ingestion (`app/pdf_ingest.py`, optional; not imported by the API; `pip install -r requirements-pdf.txt`): `pdf_to_doc(path, doc_id, acl)` returns a corpus document that goes through the normal `ingest_corpus` path, so every chunk (body text, table text, OCR text) inherits the document ACL and is redacted exactly like other chunks. It reads text with pdfplumber (column-aware order, header/footer lines repeating on >= 3 pages removed, section breadcrumbs from font size/weight), turns ruled tables into a markdown paragraph plus one sentence per row, and OCRs pages with no usable text layer (Tesseract at 300 DPI). Needs the `tesseract` binary (`brew install tesseract` / `apt-get install tesseract-ocr`); without it a scanned page raises `OcrUnavailable` rather than being dropped. The production Docker image does not install Tesseract. Measurements and limits (3-column reading order is handled, unruled tables are not detected, scans are not scored): [evals/results/2026-10-10-pdf-real-corpus/table.md](../evals/results/2026-10-10-pdf-real-corpus/table.md); corpus fetcher and manifest: [evals/pdf_corpus](../evals/pdf_corpus).
- PII redaction (`app/redact.py`, regex + Luhn): applied at ingest and to every returned result and answer. Placeholders look like `[REDACTED:EMAIL]`.
- Degradation: embedder, vector-store, reranker and LLM calls sit behind circuit breakers (`BREAKER_THRESHOLD`, `BREAKER_RESET_S`). Responses include `degraded` and `source` (`full_rag`, `retrieval_only`, `keyword_fallback`, `vector_order`, `unavailable`). The keyword fallback reads redacted chunk text mirrored in SQL and enforces the same AND-across-levels ACL rule. Permission or sync uncertainty never degrades; it stays fail-closed, and a fail-closed `/query` answers HTTP 503 with the fixed body `{"error": "retrieval unavailable", "request_id": ...}` (never results), so clients and load tests see the outage. Every cause (pending or rebuild-required permission state, index fingerprint mismatch, SQL lock or outage, audit failure, keyword fallback also down) is global state or the caller's own request, so the 503 is the same for every caller and query and reveals nothing about which documents exist. A query that matches nothing, or only documents the caller cannot read, is still 200 with the same empty body.
- `corpus/permissions.jsonl` is a patch feed: a missing document leaves its stored permissions unchanged. A row with `{"doc_id":"...","deleted":true}` explicitly deletes that document. Set `PERMISSIONS_BACKEND=gdrive` to use Drive snapshots and changes instead. Drive file IDs must match the ingested corpus `doc_id` values; the service account needs Drive metadata read access, and Drive email/domain permissions must map to the signed JWT `sub` and `groups` claims. A failed reconciliation keeps reads blocked until a successful reconciliation repairs state.
- Drive's change page token is saved in SQL in the same transaction as ACL updates. An initial sync snapshots the permissions of known ingested file IDs. Reingestion clears the saved token and causes the next sync to take a fresh baseline. An optional webhook at `/webhooks/drive` accepts notifications only when the configured channel ID, channel token, and resource ID all match; polling remains enabled as the recovery path.
- Permission sync updates access for already indexed content; it does not fetch or restore document contents. Reingest after changing the permission source/backend or moving to a different physical SQL/vector store. After a document is deleted from the index, restoring its access requires reingesting its content.
- The API polls the configured feed every `SYNC_INTERVAL_S` seconds (default `10`; set `0` to disable). `POST /sync`, restricted to the `security` group, runs a pass. Do not run the standalone sync script concurrently with a live API: it does not share the API's in-process vector-store lifecycle. Use `POST /sync` for a live process.
- Cache entries are bounded and keyed by permission revision, principal scope, exact query, and `k`. They are local to a process; the SQL revision is used to make stale entries miss after reconciliation.
- Request logs: every `/query` request, rejected ones included, writes exactly one JSON line on the `permrag` logger and returns its id in the `X-Request-ID` header. It is a header on every response; the 503 body also carries it so a client can quote it, while the denied and no-match 200 bodies stay byte-identical. Fields: `request_id`, `outcome`, `retrieve_ms`, `llm_ms`, `total_ms`, `tokens_in`, `tokens_out`, `tokens_cached`, `est_cost_usd`, `returned`, `denied`, `unverified_citations` (count of `[id]` citations naming a document that was not retrieved), `source`, `degraded`. Records hold ids, counts and timings only, never query, answer or document text. `outcome` is `ok`, `no_results`, `degraded` (keyword fallback), `llm_fallback` (retrieval-only: provider failure, open LLM breaker or spend cap), `failed_closed` (503) or `bad_request` (401, 422, 405). There is no `rate_limited` outcome: the platform has no rate limiter. Tracebacks logged by `log.exception` come from dependencies and are not scrubbed, so a dependency that echoed request text would show it there.
- `/audit` (security group) also returns `ops`: p50/p95 latency (nearest-rank, over requests that reached retrieval), `cost_per_day_usd`, per-outcome counts and rates, and `failure_rate`. This is computed in process memory, not from the audit table: `audit_log` has no outcome, latency or cost columns and adding them is a schema migration. It therefore resets on restart and is per worker (ROADMAP B12).
- `DAILY_BUDGET_USD` (default `5.0`, `0` disables the LLM) caps estimated spend per UTC day. When spent-so-far plus a worst-case projection for the next call would pass the cap, the LLM is skipped: the response is `source: retrieval_only`, `degraded: true`, with a `note`; the log says `llm_fallback`. Empty results never carry the note, so a no-match answer stays indistinguishable from a denied one. Without `ANTHROPIC_API_KEY` there is no spend and the cap is not consulted.
- LLM outage: `generation.answer` still returns a plain `str` and still falls back to the extractive answer on a provider error, but the string is a `Generated` subclass whose `failed` flag and `usage` are readable. Retrieval turns `failed` into an exception inside the LLM circuit breaker, so a real outage reports `retrieval_only` and, after `BREAKER_THRESHOLD` failures, opens the breaker instead of reporting `full_rag` forever.
- Validation errors (422) no longer echo the rejected input; the default handler did, and a lone surrogate in it turned the 422 into a 500.
- Red-team suite: `tests/test_redteam.py` runs the shared `platform/tests/redteam/attacks.json` (84 of its 89 attacks apply here) against the real app with a mocked model, including document-borne payloads ingested into the index. It asserts no restricted canary, no system prompt, escaping intact and the documented status codes. **A mocked model cannot prove semantic jailbreak resistance.**
- Audit records retain identities, returned IDs and counts; new query fields contain only `[redacted]`. `/audit` masks legacy query columns too, without rewriting history.
- `denied_count` is a count sampled from the unfiltered top-k vector results for that query. It is not a count of every forbidden chunk in the corpus and is not a comprehensive exposure metric.
- `config/postgres_rls.sql` is an example policy and is not active end-user authorization in the runtime. Runtime authorization comes from the application's verified principal and ACL checks.

The optional benchmark report in [reports/benchmark.json](reports/benchmark.json) records a run against the small offline fixture. Reproduce it with `python scripts/benchmark.py --iterations 20 --output reports/benchmark.json` and inspect the generated JSON for the run's environment, sample count, and results. Measurements are local observations, not production capacity or a security proof.

The tests exercise local code paths and fakes. They do not establish resistance to every prompt injection or side channel, nor validate a live Google account, external Qdrant server, PostgreSQL deployment/RLS, identity provider, or LLM integration. Consult [SECURITY.md](SECURITY.md) before deployment.

## Repository map

`app/` contains the API, identity verification, retrieval, ingestion, synchronization, SQL store, and connector adapter. `scripts/` contains local setup and demo commands. `corpus/` contains demo documents and the synthetic permission feed. `tests/` contains the offline test suite. `config/postgres_rls.sql` is an optional example, not an activated runtime control.

### Local models (optional)

```sh
pip install -r requirements-models.txt   # adds fastembed (ONNX, no PyTorch); never needed for tests or CI
export EMBED_BACKEND=fastembed            # BAAI/bge-small-en-v1.5, 384-d; MIN_SCORE defaults to 0.55
export SPARSE_BACKEND=splade              # optional: SPLADE++ learned-sparse hybrid (RRF fusion)
export RERANKER=fastembed                 # optional: ms-marco MiniLM cross-encoder over 25 filtered candidates
python scripts/ingest.py                  # required after changing any index-side setting
```

Models download on first use (set `MODEL_CACHE_DIR` to keep them). Changing `EMBED_BACKEND`, `EMBED_MODEL`, `EMBED_DIM` or `SPARSE_BACKEND` without reingesting fails closed through the embedding fingerprint (the sparse model is part of the fingerprint's model string); `RERANKER` is query-side only and needs no reingest. Hybrid collections name their vectors (`dense`, `sparse`), set no Qdrant IDF modifier, and are created by ingest.

- **Hybrid** runs a dense query and a sparse re-score of its candidates as two separate ACL-filtered queries and fuses them client-side with reciprocal rank fusion (`k=2`, Qdrant's default). Exact score ties share a rank and the final order is (-fused score, chunk id), so fusion depends only on the visible candidates' scores and ids, never on insertion order (which a hidden document can shift; T27). The sparse leg may only re-rank dense-qualified candidates. SPLADE was chosen over BM42 because BM42 scores only make sense with Qdrant's IDF modifier, a collection-wide statistic.
- **Rerank** takes `RERANK_CANDIDATES` (25) ACL-filtered chunks, scores each (query, chunk) pair, drops scores below `RERANK_MIN_SCORE` and returns the top `k`. If the reranker fails, retrieval falls back to vector order and the response is `degraded` with `source: "vector_order"`; the reranker has its own circuit breaker.
- **Embedding cache** (`EMBED_CACHE_PATH`, fastembed only): ingest reuses vectors for unchanged text, keyed by sha256(text) plus backend/model/dimension. Queries never touch it, so one user's query cannot speed up another's. Each successful ingest prunes entries whose text is no longer in the corpus, so a deleted document's vector does not persist. Keys include the fastembed version. The file holds vectors of redacted chunk text, so treat it like the index.
- **Floors are calibrated per model.** bge cosine similarity between unrelated texts is about 0.43-0.54, so the hash embedder's `MIN_SCORE=0.1` would return anything; the fastembed default is 0.55. In hybrid mode the sparse leg only re-scores chunks that already clear the dense `MIN_SCORE` (nested prefetch), so it can re-rank but never add a sub-floor chunk.
- **Measured, not assumed:** [rung results](../evals/results/2026-10-10-platform-rungs/table.md) (`python scripts/rung_benchmark.py`). On the small synthetic corpus dense retrieval is the clear win on paraphrases; hybrid and the reranker did not beat it, and the async path (`retrieval.aretrieve`, executor-backed, principal passed explicitly) did not improve concurrent p95, so it is not wired into `/query`.

### Hierarchy and storage verification

Remote Qdrant collections create keyword indexes on `acl_doc`, `acl_section`,
`acl_para` and `doc_id` before upload, store payload/original vectors on disk, and
configure int8 scalar quantization in RAM. Embedded Qdrant does not implement
those indexes or compression; its tests establish permission correctness only.

Document permission sync uses filtered `set_payload` in batches of up to 1,000
doc IDs with equal grants; one document is one vector write regardless of its
chunk count. Every retry replays document grants from SQL. Child restrictions
remain unchanged until reingestion. Deletion intent is persisted before remote
deletion; interrupted deletion requires full reingestion rather than reopening
an incomplete index. Sync still reads the SQL chunk/policy mirror; 100k-document
latency, memory and revocation targets have **not** been measured.

To verify a real server using a unique synthetic collection (removed afterwards):

```bash
python ../evals/verify_qdrant_storage.py --url http://localhost:6333
```

This checks engine hierarchy filtering, bulk revocation, payload indexes and
storage configuration; it does not measure quantization savings or ANN recall.
The 2026-10-07 check used Qdrant server/client 1.18.0. Production uses a configured
`QDRANT_URL`; the public AWS demo runs the small stdlib reference app instead.

Platform SQL audit records rely on trusted database storage. They are appended by the application but do not have the reference backend’s hash chain or protection against a privileged database writer rewriting history.
