# Scale measurement — 2026-10-08

**Filtered engine retrieval met the local latency target; permission reconciliation missed its target.**
This is one local run with 100,000 synthetic documents × 15 chunks = 1,500,000
points. It uses the actual Qdrant collection configuration and search function,
and the actual `sync_once` over a populated temporary SQLite permission mirror.

| Visibility | Visible points | Samples | p50 | p95 | p95 < 100 ms |
|---|---:|---:|---:|---:|---|
| 1% | 15,000 | 75 | 2.91 ms | 4.15 ms | met locally |
| 10% | 150,000 | 75 | 3.28 ms | 4.45 ms | met locally |
| 60% | 900,000 | 75 | 3.33 ms | 8.00 ms | met locally |

| Operation | Measured | Target / interpretation |
|---|---:|---|
| Streamed engine ingest + SQL mirror | 274.03 s | Not production `ingest_corpus()` |
| Optimizer settling after ingest | 20.05 s | Green, all 1.5M vectors indexed before queries |
| Actual single-document reconciliation | 102.29 s | Full mirror scan/replay despite one changed document |
| Actual 10k-document folder reconciliation | **104.92 s** | **missed < 60 s target** |
| Unauthorized returned chunks | 0 across 225 query probes | Nonempty result control passed 225/225; outsider denied |
| Granted points remaining in revoked document/folder | 0 / 0 | Exact count after reconciliation |

The folder requested 10,000 document revocations, of which 9,999 changed: one was
already revoked in the preceding single-document check. This is reported rather
than counted as 10,000 newly changed documents. A miss under this slightly reduced
workload does not support the full target.

## Method and resources

Qdrant server/client 1.18.0, HTTP, 384-dimensional local hash embeddings, batch 512.
A dedicated Docker container was capped at 4 CPUs and 5,500 MiB, on a macOS host
with 48 GiB memory. The 100k documents have deterministic repeated policy text,
independent document/section/paragraph grants and a fixed generator digest.
Embedding templates are reused. SQL mirror insertion is streamed through Core
inserts; **the application's full-materialization ingest path is not benchmarked**.

Queries are sequential and include HTTP transport. Five warmups per visibility
bucket precede 75 measured queries. Exact filtered counts verify the bucket sizes.
The leak oracle derives ACLs from generated IDs, independently of returned ACL
payloads. A nonempty-results control rejects vacuous denial; this is not semantic
recall. Background development/checks occurred during ingestion; this was not a
quiet-host capacity experiment. No model API or AWS scale resources were used.

- Qdrant container cgroup memory before queries: 1.88 GiB.
- Container cgroup memory after reconciliation: 3.82 GiB; whole-run peak: 4.40 GiB.
- Harness peak RSS on macOS: 4.52 GiB, including SQL reconciliation and cached vectors.

Container memory includes page cache; it is **not** a measurement of only resident
quantized vectors or isolated RSS. Harness and Qdrant memory are distinct processes.
These observations do not establish compressed-memory savings or a total 8-GiB
application deployment budget.

## What the miss means

Document-filtered vector writes avoid per-chunk round trips, but reconciliation
still loads all chunk and policy rows and replays all document grants to recover
from partial remote writes and SQL rollback. At 1.5M chunks, this dominates the
measured operation. The next scale change should preserve the durable barrier and
retry guarantees while tracking pending document mutations explicitly, instead
of scanning/replaying the full mirror. That fix is not implemented in this batch.

## Reproduce

Use a disposable local Qdrant 1.18.0 server and the platform dependencies. From
repository root:

```sh
python evals/benchmark_scale.py --url http://127.0.0.1:6333 \
  --documents 100000 --chunks 15 --queries 75 \
  --container YOUR_OWN_TEST_CONTAINER --output /tmp/scale-report.json
```

`--container` is optional and reads local cgroup memory from the named container.
The harness creates/removes only its unique `scale-<uuid>` collection and a temporary
SQLite mirror. It must not be pointed at an untrusted remote service.
For a small smoke run use `--documents 100 --chunks 3 --queries 5 --optimizer-timeout 0`.

[Raw report](report.json) contains samples, configuration, digests and source refs.
Its local base hash corresponds to public runtime ref `6ea35f04`; the executed new
harness checksum is retained. The published harness adds Linux RSS-unit normalization
after this macOS run, with identical behavior on macOS. Single-run warm synthetic
results do not establish concurrency, cold-cache latency, ANN recall, production
ingest feasibility, end-to-end API timing or general zero-leak safety.
