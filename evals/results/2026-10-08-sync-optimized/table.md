# Durable reconciliation: local 100k-document remeasurement

Same deterministic corpus as the [baseline](../2026-10-08-scale/table.md):
100,000 documents, 1.5 million chunks, and identical corpus digest. Real Qdrant
server/client 1.18.0 over HTTP, 384-dimensional local hash embeddings, batch 512;
a dedicated Docker container capped at 4 CPUs and 5,500 MiB, with a temporary
SQLite mirror populated by the streamed harness.

| Measurement | Baseline | Durable journal |
|---|---:|---:|
| Actual single-document `sync_once` | 102.29 s | **0.63 s** |
| Actual folder `sync_once` | 104.92 s | **13.31 s**, meets local <60 s target |
| Granted points remaining after document / folder revocation | 0 / 0 | 0 / 0 |
| Search p95, 1% visibility | 4.15 ms | 2.13 ms |
| Search p95, 10% visibility | 4.45 ms | 2.25 ms |
| Search p95, 60% visibility | 8.00 ms | 58.88 ms |
| Streamed engine + SQL mirror ingest | 274.03 s | 280.45 s |
| Optimizer settling after ingest | 20.05 s | 35.06 s |
| Unauthorized chunks across 225 probes | 0 | 0 |
| Nonempty positive controls | 225/225 | 225/225 |

Both folder runs requested 10,000 revocations, with 9,999 newly changed because
one document had already been revoked by the preceding check. Exact filtered
counts found no remaining granted points; an outsider returned no chunks.
The baseline miss remains published. This improvement reduces full ORM hydration
and all-document vector replay; **global scalar SQL integrity checks remain**.
It does not make reconciliation independent of total mirror size.

## What this run establishes

The harness invokes the actual application `sync_once`. Durable intents survive
partial vector writes or failed SQL completion, including an omitted document in
subsequent patches. Separate [recovery regressions](../../../docs/SYNC_RECONCILIATION.md)
exercise those failures; the benchmark measures successful reconciliation latency.

Five warmups precede 75 sequential queries per visibility bucket. Background
mutation testing and development were active; this is not a quiet-host capacity
experiment. The higher 60% search p95 precludes claiming a query speedup. The
oracle derives ACLs independently from generated IDs; nonempty controls prevent
vacuous denial, but do not establish semantic recall or ANN score isolation.

Container cgroup memory was 2.40 GiB before queries and 3.99 GiB after
reconciliation, including page cache. Harness peak RSS was 0.71 GiB. These are
separate processes and do not establish isolated quantization savings or a total
application deployment budget. No optimized whole-run container peak is reported.

The streamed harness bypasses production `ingest_corpus` materialization. No
end-to-end API, generation, audit, concurrency, cold-cache, semantic recall, or
production-ingestion claim follows from this single synthetic local run.
No paid cloud scale resources or model calls were used.

## Reproduce and source provenance

From repository root with platform dependencies and a disposable local Qdrant:

```sh
python evals/benchmark_scale.py --url http://127.0.0.1:6333 \
  --documents 100000 --chunks 15 --queries 75 \
  --container YOUR_OWN_TEST_CONTAINER --output /tmp/scale-report.json
```

The harness creates/removes only its unique collection and a temporary SQLite
mirror. `--container` optionally reads memory from your test container.
For a smoke run use `--documents 100 --chunks 3 --queries 5 --optimizer-timeout 0`.

[Raw report](report.json) retains all samples, corpus digest, the executed harness
checksum and a checksum for every runtime source file. `source_commit` is the
local base before these uncommitted runtime changes; the source checksums pin
what was actually executed. Check out `codex/sync-scale` to reproduce the journal
implementation. The fresh local gates passed: 358 platform tests, 98.97% combined
branch coverage, 1,646/1,661 mutants killed, 15 reviewed equivalents and zero
unresolved mutation outcomes. Passing local gates does not imply PR CI success.
