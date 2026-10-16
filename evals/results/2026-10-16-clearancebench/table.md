# ClearanceBench leak eval of the reference engine (2026-10-16)

Dataset `NagaYu/clearance-bench` @ `0ff4169c8085`, seed 20261016. 14067 chunks, 600 principals, 240 queries x evaluation times [0.0, 250.0, 500.0, 1000.0], top-10. Engine: `app/permission_rag.py` (BM25 over the visible set). Index build 3.02 s; query median 49.8 ms, p95 61.4 ms.

## Direct leaks (lower is better)

| Queries / asked by | Attempts | Unreadable chunks in results (by id) | By canary | Returned anything |
|---|---|---|---|---|
| normal/asker | 632 | 0/6320 = 0.00% (Wilson 95% CI 0.00-0.06%) | 0 | 632/632 = 100.00% (Wilson 95% CI 99.40-100.00%) |
| normal/cross | 1896 | 0/18960 = 0.00% (Wilson 95% CI 0.00-0.02%) | 0 | 1896/1896 = 100.00% (Wilson 95% CI 99.80-100.00%) |
| probe/asker | 328 | 0/3280 = 0.00% (Wilson 95% CI 0.00-0.12%) | 0 | 328/328 = 100.00% (Wilson 95% CI 98.84-100.00%) |
| probe/cross | 984 | 0/9840 = 0.00% (Wilson 95% CI 0.00-0.04%) | 0 | 984/984 = 100.00% (Wilson 95% CI 99.61-100.00%) |
| **All** | **3840** | **0/38400 = 0.00% (Wilson 95% CI 0.00-0.01%)** | **0** | |

## Existence, revocation and the oracle

- Score identity (results over the full index equal results over an index holding only the asker's readable chunks, scores included): 0 diffs in 20 samples.
- Revocation, 50 cases: target retrieved before revocation 37, target returned after 0, unreadable chunks after 0, precondition unmet 0.
- Oracle vs the generator's own ground truth: probe target unreadable for its asker 326/328 = 99.39% (Wilson 95% CI 97.80-99.83%); revocation `n_chunks_lost` reproduced exactly 50/50 = 100.00% (Wilson 95% CI 92.87-100.00%).
- Probe-target recall by a principal who may read it, exact chunk: hit@1 57/320 = 17.81% (Wilson 95% CI 14.01-22.38%), hit@4 125/320 = 39.06% (Wilson 95% CI 33.88-44.51%), hit@10 191/320 = 59.69% (Wilson 95% CI 54.23-64.92%); same document (any chunk of the target's document; metric added after the first smoke run): hit@1 212/320 = 66.25% (Wilson 95% CI 60.90-71.21%), hit@4 280/320 = 87.50% (Wilson 95% CI 83.43-90.68%), hit@10 312/320 = 97.50% (Wilson 95% CI 95.15-98.73%).

## Controls (the oracle must see planted bugs)

- `NoFilter` (no ACL check): probe queries leaking 328/328 = 100.00% (Wilson 95% CI 98.84-100.00%).
- `read_up` mutant, targeted probes: leaking 400/400 = 100.00% (Wilson 95% CI 99.05-100.00%); same probes on the real engine 0/400 = 0.00% (Wilson 95% CI 0.00-0.95%).
- `embargo` mutant, targeted probes: leaking 400/400 = 100.00% (Wilson 95% CI 99.05-100.00%); same probes on the real engine 0/400 = 0.00% (Wilson 95% CI 0.00-0.95%).
