# Changelog

## Unreleased

- Fix: the daily spend cap was check-then-act, so concurrent requests could all pass it before any of them spent. Each LLM call now reserves its worst-case cost under the lock and settles to the actual cost afterwards (a failed call releases the hold). Per process; resets on restart.
- Behaviour change: with the LLM off by configuration (no `ANTHROPIC_API_KEY`, or `DAILY_BUDGET_USD=0`) a non-empty answer reports `source: "retrieval_only"`, `degraded: false`, outcome `ok`, instead of claiming `full_rag`. A zero budget no longer reports the "budget reached" note. Empty (no-match or denied) responses are unchanged.
- Explicit outbound timeouts: `QDRANT_TIMEOUT_S` (5), `DB_CONNECT_TIMEOUT_S` (5, Postgres), `DRIVE_TIMEOUT_S` (10). `/readyz` now also requires the Qdrant collection to exist and answers 503 with the same body when it does not or Qdrant is unreachable.
- Every `permrag` log record, tracebacks included, carries the request id (`record.request_id`, printed in the server log format).
- The retired-model fallback warning now says to pin a current dated id in `GENERATION_MODEL`.
- A fail-closed `/query` now returns HTTP 503 with `{"error": "retrieval unavailable", "request_id": ...}` instead of a 200 with the empty body, so outages show in client error rates and load tests. Logging and the audit row are unchanged. Denied and no-match queries still return the identical 200 empty body.
- Queries no longer hold the permission write lock while they retrieve and generate. They read the permission state lock-free and re-check revision and barrier flags in the short transaction that writes the audit row; a revocation that lands mid-query fails that query closed instead of queueing behind it. Queries no longer serialize on the lock, which matters when generation or the database is slow; on the keyless hash-embedder profile throughput is unchanged because one process is CPU-bound. Side effect: queries that start while a reconciliation is pending fail closed immediately instead of queueing behind it; unchanged JSONL polls no longer start one.
- `sync_once` no longer raises the read barrier (pending, revision + 1) when a JSONL source already matches the applied SQL mirror, so the periodic poll stops failing in-flight queries closed. Any difference, saved intent, pending barrier or validation failure still takes the full barrier path. Drive polls keep the barrier (ROADMAP B19).
- Fix (security, S1-class): hybrid RRF now runs client-side with competition ranking and a (-score, chunk id) order, so exact score ties no longer let hidden documents shift a visible chunk's fused score through the engine's insertion order (T27).
- Add one JSON request log line per `/query` (ids, counts, timings; no text), an `X-Request-ID` header, an in-process `ops` summary in `/audit`, and a `DAILY_BUDGET_USD` spend cap that falls back to retrieval-only.
- Make provider failures visible to the LLM circuit breaker without changing `generation.answer`'s string result; 422 responses no longer echo rejected input.
- Add a red-team suite driven by `platform/tests/redteam/attacks.json` (mocked model; structural claims only).
- Add opt-in local models: `EMBED_BACKEND=fastembed` (bge-small, ingest-side embedding cache), `SPARSE_BACKEND=splade` (hybrid RRF with the ACL filter on both prefetches and no IDF modifier), `RERANKER=fastembed` (cross-encoder over ACL-filtered candidates, vector-order fallback). Defaults unchanged; `scripts/rung_benchmark.py` measures each rung against the leak gates. Add `retrieval.aretrieve` (not wired into `/query`: no p95 gain).

- Preserve section restrictions during permission updates and persist reconciliation barriers across failures.
- Serialize retrieval, generation, and audit commits against permission mutations; prevent superseded work from clearing a newer barrier.
- Fail closed on malformed ACLs, invalid identities, missing indexed chunks, and audit failures.
- Scope bounded caches by permission revision, identity, exact query, and requested result count.
- Reconcile permissions at startup and through polling, administrative triggers, and authenticated Drive notifications.
- Persist Drive checkpoints transactionally with permission changes and paginate complete permission snapshots.
- Add isolated leakage, latency, and revocation measurements, plus security regression tests.
- Render demo answers and audit data safely as text and discard stale responses after identity changes.
- Enforce at least 96% combined application and command-script branch coverage in CI.
- Add a nonroot persistent Docker image and functional container checks for authentication, access filtering, restart persistence, and public-key-only verification.
- Add Linux mutation testing and validate complete mutation reports before accepting results.
