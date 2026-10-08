# Public-input retrieval isolation and adapter evidence — 2026-10-08

Measured locally on Python 3.12.4. Base commit `f8a01244506087035a3a79dd23d0382b989d1eda` plus the newly authored kit; the report's `implementation_sha256` pins the exact executed Python implementation, including reference/mutant source. No hosted competitor package was executed.

| Adapter/input | Visibility-failing probes | Isolation-failing probes | Required exact-content hits | Malformed ACL controls | Warmed revocation controls |
|---|---:|---:|---:|---:|---:|
| Reference BM25 / synthetic controls | 0/192 | 0/192 | 18/18 | 12/12 safe | 1/1 passed |
| Reference BM25 / public text + fictional ACLs | 0/2088 | 0/2088 | 197/197 | not in this fixture | 1/1 passed |
| Independent term-overlap example / controls | 0/192 | 0/192 | 18/18 | 12/12 safe | 1/1 passed |
| Independent term-overlap example / public fixture | 0/2088 | 0/2088 | 197/197 | not in this fixture | 1/1 passed |

All four runs had zero operation errors. Public fixture: 87 externally authored paragraph texts × 8 principal scopes × k=1,3,20 = 2088 probes. Queries are the source paragraph's exact text, not independently authored semantic questions. A positive control requires that allowed paragraph in top-20. Visibility failures count probes containing a forbidden or unknown ID, not distinct leaked documents. Isolation compares ordered IDs and exact scores over full versus independently authorized visible-only corpora. Scores here are deterministic BM25 or term-overlap scores, not ANN scores.

The source is the [National Archives Constitution transcription](https://www.archives.gov/founding-docs/constitution-transcript). Text-only extraction, rights provenance, source HTML hash and fictionally assigned ACL rules are in the [input manifest](../../kit/public_constitution.manifest.json) and [kit README](../../kit/README.md). Input digest: `e4f60a85e8239471d82bb3212e503ac817a175f137f3a2ed9ddebc6e62f3cec6`. No genuine permissions are inferred from public text. This is a small external-text regression run, not recovered private legal input, a held-out underwriting corpus, or a 100k-document result.

## Fault sensitivity

Eight of eight faulty reference adapters failed on both fixtures, without crashes. Public-fixture observations:

| Fault | Visibility-failing probes | Isolation-failing probes | Positive-control misses | Operation errors |
|---|---:|---:|---:|---:|
| NoFilter | 1861/2088 | 2088/2088 | 0/197 | 0 |
| GlobalIdf | 1382/2088 | 2067/2088 | 0/197 | 0 |
| PostFilterTopK | 1012/2088 | 2067/2088 | 0/197 | 0 |
| StarSubstring | 1173/2088 | 1809/2088 | 0/197 | 0 |
| GroupPrefix | 497/2088 | 774/2088 | 0/197 | 0 |
| SharedCache | 1617/2088 | 1809/2088 | 33/197 | 0 |
| AnyLevelGrants | 1836/2088 | 2088/2088 | 0/197 | 0 |
| FlattenIntersection | 0/2088 | 0/2088 | 32/197 | 0 |

`FlattenIntersection` causes false denials: it is caught by positive controls, despite zero disclosure/isolation failures. The original `GlobalIdf` and `PostFilterTopK` faults predate hierarchy and ignore child ACLs too; these mixed-hierarchy kills do not isolate only their named defect. A separately executed **flat-ACL IDF control** had 0/192 visibility failures and 192/192 isolation failures on controls, and 0/2088 visibility failures with 2067/2088 isolation failures on public text. Thus the gate observes a score/ranking side channel even when every returned ID is authorized.

The always-empty adapter is rejected with 18 required-hit misses and a failed revocation warm-up. Tests also reject duplicate/unknown IDs, nonfinite/boolean scores and backend exceptions. **20 kit tests passed.** The Node promptfoo provider shim was directly exercised for a passing report and subprocess-error propagation; full promptfoo CLI/SDK integration remains unexecuted.

## Reproduce

From the repo root, using Python 3.12+:

```sh
python3 -m pytest -q app/test_leak_kit.py
python3 -m evals.kit.run --mutants
python3 -m evals.kit.run --fixture evals/kit/public_constitution.json --mutants
python3 -m evals.kit.run --adapter evals/kit/example_adapter.py
python3 -m evals.kit.run --adapter evals/kit/example_adapter.py \
  --fixture evals/kit/public_constitution.json
```

Raw reports: [controls](controls.json), [public](public.json), [independent example controls](example.json), [independent example public](example-public.json). Each records counts, limited failing IDs/scores/scope examples, Python version and source/implementation/input hashes. No model calls, cloud resources, or original frozen-eval changes were needed.

Limits: finite exact-content probes; fictional static permission overlays plus one controlled revocation; no real identity/provider synchronization test; no stochastic ANN guarantee; no answer safety test. Source-to-chunk boundary behavior is covered separately by application hierarchy tests, not by this canonical-chunk interface. No unfamiliar engineer has performed an unassisted integration, so **L4 is not achieved**. The new kit supplements the existing frozen 1350-probe evaluation and does not replace or weaken it.
