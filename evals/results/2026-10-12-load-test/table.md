# Platform load test (2026-10-12)

Keyless default config, 12-chunk synthetic corpus, Apple M3 Max (16 cores), Python 3.12.13, one laptop shared with
other running jobs: the host load average swung between 8 and 39 during these runs, so treat every cell as noisy
(up to about 3x). Main matrix: 5 s warm-up (excluded) + 60 s measured per cell. A/B matrix: 5 s + 30 s. Seed 7.
`error rate` counts only transport errors, non-200 and `source: unavailable`. Fail-closed responses are HTTP 200 with
the canonical empty body, so they are counted from the server log (`srv fail-closed`; `-` for the main matrix, which
ran before that column existed; whole-log totals are in the fail-closed section below).

## Main matrix (unique queries = cache misses; repeated = cache hits; healthz = client ceiling)

| config | VUs | req/s | p50 ms | p95 ms | p99 ms | error rate | srv fail-closed | srv total p50 | srv retrieve p50 | srv outside-retrieve p50 | leaks/results | Wilson95 upper |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1worker-repeated-queries | 1 | 153.0 | 5.5 | 11.9 | 30.7 | 0.00% | - | 1.7 | 0.0 | 1.7 | 0/4620 | 8.31e-04 |
| 1worker-repeated-queries | 5 | 210.2 | 18.1 | 54.4 | 128.3 | 0.00% | - | 4.3 | 0.0 | 4.3 | 0/7009 | 5.48e-04 |
| 1worker-repeated-queries | 10 | 547.6 | 4.2 | 65.6 | 226.5 | 0.00% | - | 1.6 | 0.0 | 1.6 | 0/19328 | 1.99e-04 |
| 1worker-repeated-queries | 25 | 513.0 | 17.5 | 155.2 | 649.1 | 0.00% | - | 12.6 | 0.0 | 12.6 | 0/18097 | 2.12e-04 |
| 1worker-repeated-queries | 50 | 222.9 | 166.3 | 517.6 | 1303.2 | 0.00% | - | 96.3 | 0.0 | 96.2 | 0/7844 | 4.90e-04 |
| 1worker-unique-queries | 1 | 368.7 | 2.6 | 3.3 | 4.2 | 0.00% | - | 1.7 | 0.6 | 1.1 | 0/13264 | 2.90e-04 |
| 1worker-unique-queries | 5 | 395.2 | 4.5 | 42.8 | 153.2 | 0.00% | - | 2.2 | 0.7 | 1.4 | 0/14818 | 2.59e-04 |
| 1worker-unique-queries | 10 | 199.1 | 20.7 | 167.6 | 707.3 | 0.00% | - | 5.9 | 1.3 | 3.9 | 0/7994 | 4.80e-04 |
| 1worker-unique-queries | 25 | 145.5 | 80.1 | 655.6 | 1759.3 | 0.00% | - | 44.1 | 2.0 | 41.9 | 0/5589 | 6.87e-04 |
| 1worker-unique-queries | 50 | 139.5 | 262.0 | 892.7 | 2189.7 | 0.00% | - | 166.2 | 2.0 | 163.8 | 0/5506 | 6.97e-04 |
| 2workers-unique-queries | 1 | 106.5 | 7.0 | 23.8 | 48.1 | 0.00% | - | 3.1 | 1.0 | 2.1 | 0/3862 | 9.94e-04 |
| 2workers-unique-queries | 5 | 261.8 | 7.6 | 65.2 | 195.1 | 0.00% | - | 3.3 | 0.8 | 2.5 | 0/9653 | 3.98e-04 |
| 2workers-unique-queries | 10 | 390.4 | 4.7 | 98.2 | 464.7 | 0.00% | - | 3.1 | 0.7 | 2.4 | 0/15614 | 2.46e-04 |
| 2workers-unique-queries | 25 | 423.3 | 5.0 | 259.4 | 1086.8 | 0.00% | - | 3.2 | 0.7 | 2.4 | 0/16485 | 2.33e-04 |
| 2workers-unique-queries | 50 | 429.6 | 30.9 | 341.2 | 1859.3 | 0.00% | - | 25.5 | 0.8 | 24.5 | 0/16863 | 2.28e-04 |
| client-ceiling-healthz | 50 | 2505.1 | 14.9 | 60.8 | 88.0 | 0.00% | - | - | - | - | 0/0 | 1.00e+00 |

The client ceiling on `/healthz` is 2505 req/s, about 6x the best `/query` cell, so the Python client is not the limit.

## A/B: one process vs two (30 s cells, back to back)

`2proc-shared-sqlite`: two uvicorn processes, one SQLite file, separate Qdrant copies (Qdrant local mode locks its
directory). `2proc-separate-sqlite`: each process has its own SQLite file and Qdrant copy. The client round-robins VUs
over the processes. `1proc-again` repeats `1proc` at the end; it ran at about a third of the speed because the host load
average rose from 8 to 33, which is the noise floor of this whole exercise.

| config | VUs | req/s | p50 ms | p95 ms | p99 ms | error rate | srv fail-closed | srv total p50 | srv retrieve p50 | srv outside-retrieve p50 | leaks/results | Wilson95 upper |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ab-1proc-again | 10 | 147.7 | 43.8 | 168.0 | 507.9 | 0.00% | 1 | 7.2 | 1.8 | 5.1 | 0/2941 | 1.30e-03 |
| ab-1proc-again | 25 | 151.8 | 85.4 | 562.4 | 1698.5 | 0.00% | 3 | 30.3 | 1.9 | 28.3 | 0/2974 | 1.29e-03 |
| ab-1proc-again | 50 | 161.6 | 229.6 | 707.9 | 1918.2 | 0.00% | 145 | 140.2 | 1.8 | 138.3 | 0/3074 | 1.25e-03 |
| ab-1proc | 10 | 408.7 | 4.4 | 96.6 | 459.2 | 0.00% | 0 | 2.1 | 0.7 | 1.3 | 0/8114 | 4.73e-04 |
| ab-1proc | 25 | 421.1 | 22.3 | 111.5 | 1108.1 | 0.00% | 1 | 18.6 | 0.8 | 17.8 | 0/8188 | 4.69e-04 |
| ab-1proc | 50 | 438.8 | 78.0 | 133.3 | 1140.0 | 0.00% | 2 | 54.5 | 0.8 | 53.6 | 0/8692 | 4.42e-04 |
| ab-2proc-separate-sqlite | 10 | 288.8 | 24.1 | 84.3 | 229.7 | 0.00% | 3 | 6.7 | 1.8 | 4.5 | 0/5720 | 6.71e-04 |
| ab-2proc-separate-sqlite | 25 | 287.3 | 41.3 | 279.5 | 931.3 | 0.00% | 28 | 9.5 | 1.8 | 7.3 | 0/5592 | 6.87e-04 |
| ab-2proc-separate-sqlite | 50 | 294.0 | 86.6 | 645.5 | 1587.0 | 0.00% | 9 | 34.7 | 1.9 | 32.6 | 0/5713 | 6.72e-04 |
| ab-2proc-shared-sqlite | 10 | 389.2 | 4.6 | 99.2 | 463.4 | 0.00% | 6 | 3.2 | 0.7 | 2.4 | 0/7759 | 4.95e-04 |
| ab-2proc-shared-sqlite | 25 | 319.6 | 8.0 | 366.0 | 1395.0 | 0.00% | 374 | 4.7 | 0.9 | 3.7 | 0/6019 | 6.38e-04 |
| ab-2proc-shared-sqlite | 50 | 168.1 | 140.1 | 1313.6 | 2764.6 | 0.00% | 9 | 103.9 | 1.9 | 101.8 | 0/3281 | 1.17e-03 |

## Permission leaks

Every returned result in every cell was checked against the independent oracle (document AND section AND paragraph
ACL, 10 personas): 0 leaks of 234,613 returned results over all cells. The last column is the per-cell Wilson 95% upper bound on the leak rate
(it shrinks with n; 0/20000 gives 1.9e-4). Mean results per response were 0.6 (some personas are denied by design).

## Fail-closed responses seen in the server logs (whole life of each server process)

| server log | what ran | requests logged | failed_closed |
|---|---|---|---|
| s1 | 1-worker matrix, first half of the 2-worker matrix | 251568 | 213 (0.085%) |
| s2 | second half of the 2-worker matrix, shared SQLite | 46077 | 66 (0.14%) |
| c1 | A/B matrix, process A | 93092 | 411 (0.44%) |
| c2 | A/B matrix, second process on the SAME SQLite file | 14946 | 200 (1.3%) |
| c3 | A/B matrix, second process on its OWN SQLite file | 14664 | 19 (0.13%) |
| prof | stage-timing server, c=1 and c=25 | 45757 | 3 |

Two stage-timed requests spent 5.17 s inside `permission_transaction` before failing: SQLite's default 5 s busy
timeout. The caller receives HTTP 200 with the "No results found." body, indistinguishable from a real empty result.

## Diagnosis A: stage timers (profile_server.py, 30 s cells, ms)

| stage | c=1 mean | c=25 mean | c=25 p99 |
|---|---|---|---|
| wait to enter `permission_transaction` (UPDATE write lock) | 0.31 | **55.2** | **1192** |
| transaction body (cache check, fingerprint, embed, search, audit insert) | 1.08 | 1.53 | 3.6 |
| embed_one | 0.05 | 0.05 | 0.12 |
| vector search (filtered) | 0.36 | 0.54 | 1.36 |
| vector search (unfiltered denied-count) | 0.23 | 0.35 | 0.95 |
| write_audit | 0.35 | 0.45 | 1.25 |
| commit | 0.30 | 0.46 | 1.57 |

At c=25, 13957 requests spent 770 s in aggregate waiting to enter the transaction and 21 s inside it: about 97% of
server-side time is queueing for the lock, while the work itself barely moves. Server process CPU (ps): 86% at c=1,
108-116% at c=25 (about one core either way).

## Diagnosis B: in-process, no HTTP (inproc_bench.py, 5 s x 3 rounds, req/s median)

| variant | 1 thread | 8 threads | 25 threads |
|---|---|---|---|
| A as shipped (SQLite UPDATE lock + audit INSERT) | 405 | 464 | 245 |
| B no write lock, audit skipped (what-if, unsafe) | 653 | 347 | 333 |

With the write lock and audit write removed, extra threads still add nothing (653 at 1 thread, 333-347 at 8-25): the
process is CPU-bound in Python. Removing the lock buys +60% at 1 thread and +36% at 25 threads and removes the
1 s tail; it does not make threads scale.

## What is and is not established

- Established (this machine): latency growth and the 5 s fail-closed events come from queueing on the permission
  transaction (Diagnosis A).
- Established: one process saturates near one core, about 400-650 req/s on this 12-chunk corpus, regardless of thread
  count (Diagnosis B, CPU%). py-spy could not attach (macOS needs root), so the GIL itself is inferred, not sampled.
- Suggestive, not proven: a second process sharing the SQLite file adds no throughput and raises fail-closed responses
  (1.3% vs 0.13% with its own SQLite file); host noise is too large for a clean scaling number.
- Per-process state: each process keeps its own answer cache and `OPS` stats, so `/audit` ops sees only the worker that
  served the call (ROADMAP B12 / PLAN F5).
- Cache-hit cells are not faster than misses (50 VUs: 223 vs 140 req/s, both noisy): a hit still takes the permission
  transaction and writes an audit row; the cache saves only about 0.6 ms of retrieval.
- Corpus is 12 chunks; vector-search cost will grow with corpus size, the lock queueing will not change.
