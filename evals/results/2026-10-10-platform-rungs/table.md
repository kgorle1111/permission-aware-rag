# Platform retrieval rungs (2026-10-10)

| rung | floors | paraphrase hit@1 | paraphrase hit@4 | paraphrase MRR | lexical hit@1 | lexical hit@4 | out-of-corpus answered | leaks | hidden-doc diffs | existing suites | cold p50 / p95 ms | ingest s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | dense 0.1 | 0% | 0% | 0.00 | 100% | 100% | 50% | 0/535 | 0/321 | 13/13 | 2.45 / 6.42 | 0.05 |
| dense_floor_0.1 | dense 0.1 | 77% | 94% | 0.84 | 100% | 100% | 100% | 0/535 | 0/321 | 8/13 | 5.69 / 12.4 | 0.31 |
| dense | dense 0.55 | 74% | 91% | 0.81 | 100% | 100% | 0% | 0/535 | 0/321 | 10/13 | 5.45 / 6.96 | 0.28 |
| hybrid | dense 0.55 | 77% | 91% | 0.82 | 100% | 100% | 0% | 0/535 | 0/321 | 10/13 | 42.69 / 56.36 | 1.65 |
| hybrid_rerank_no_floor | dense 0.55, rerank -1000.0 | 71% | 89% | 0.80 | 100% | 100% | 0% | 0/535 | 0/321 | 10/13 | 83.06 / 227.67 | 2.99 |
| hybrid_rerank | dense 0.55, rerank -10.0 | 63% | 69% | 0.66 | 100% | 100% | 0% | 0/535 | 0/321 | 13/13 | 124.4 / 294.48 | 3.0 |

Existing-suite failures:

- `dense_floor_0.1`: `tests/test_revocation.py::test_doc_revocation_propagates_under_bound`, `tests/test_revocation.py::test_group_change_loses_all_group_content`, `tests/test_revocation.py::test_revocation_also_kills_cached_answers`, `tests/test_side_channels.py::test_denied_count_in_audit`, `tests/test_side_channels.py::test_forbidden_topic_indistinguishable_from_nonexistent`
- `dense`: `tests/test_revocation.py::test_doc_revocation_propagates_under_bound`, `tests/test_revocation.py::test_group_change_loses_all_group_content`, `tests/test_side_channels.py::test_denied_count_in_audit`
- `hybrid`: `tests/test_revocation.py::test_doc_revocation_propagates_under_bound`, `tests/test_revocation.py::test_group_change_loses_all_group_content`, `tests/test_side_channels.py::test_denied_count_in_audit`
- `hybrid_rerank_no_floor`: `tests/test_revocation.py::test_doc_revocation_propagates_under_bound`, `tests/test_revocation.py::test_group_change_loses_all_group_content`, `tests/test_side_channels.py::test_denied_count_in_audit`

## Concurrency (20 callers, 8 rounds, rung `hybrid_rerank`)

| mode | p50 ms | p95 ms | round wall p50 ms | wrong/empty responses |
|---|---|---|---|---|
| sequential | 97.24 | 401.29 | 2824.7 | 0 |
| threadpool_sync | 884.94 | 2093.33 | 2021.9 | 0 |
| asyncio | 970.72 | 2097.4 | 2024.7 | 0 |

Environment: macOS-26.6.2-arm64-arm-64bit, Python 3.12.13, fastembed 0.9.0, qdrant-client 1.19.1. Corpus: 37 chunks (synthetic). Probes: 35 paraphrase, 15 lexical, 50 out-of-corpus (query x user). Timings are local CPU, single run.

## How to read this

- **Small and synthetic.** 37 chunks, 35 paraphrase probes: one probe is 2.9 points, so differences of one
  or two probes are noise. Paraphrase probes share no 5-letter word stem with their target (enforced here and
  in `tests/test_rung_benchmark.py`). Numbers do not transfer to a real corpus.
- **Floors are in-sample.** `MIN_SCORE` and `RERANK_MIN_SCORE` were set just above the best score any of the
  10 out-of-corpus queries reached, using these same probes. Expect worse out of sample. "Out-of-corpus
  answered" is the share of (query, user) pairs returning at least one result; lower is better.
- **Leaks:** every query (probes, out-of-corpus, each chunk's exact text, canaries, injection wrappers) x every
  user, each returned (doc, text) checked against an independent per-level AND oracle and canary strings.
- **Hidden-doc diffs:** for guest, alice and bob, responses over the full index must equal, doc for doc and
  score for score, responses over an index that never contained their unreadable documents. This is the S1
  score-identity property measured end to end for every rung, including fusion and reranking.
- **Existing suites** are `tests/test_leakage.py`, `test_side_channels.py`, `test_injection.py`,
  `test_hierarchy.py` and `test_revocation.py`, rerun with the rung's settings. Their failures are
  `results == []` assertions (revocation, group change, denied-count audit) and the "forbidden topic equals
  nonsense" equality: they pass on the hash embedder because its noise floor returns nothing for off-topic
  text, and fail when a real embedder returns visible, semantically related chunks. The canary and
  forbidden-document assertions in those same tests pass; the leak and invariance columns are 0 on every rung.
- **Concurrency** latencies for the threaded and async modes are measured from the start of a 20-request
  burst (queueing included); `sequential` is per-request service time. Compare `threadpool_sync` with
  `asyncio`, which is the decision this table supports.
- **Batch independence.** Batched ONNX embedding shifted vectors by up to 4.3e-4 (30 of 37 texts) and changed
  12 of 37 four-decimal cosine scores versus embedding each text alone; a hidden document could perturb a
  visible chunk that way. Ingest now embeds one text per call, and hidden-doc diffs are 0 with cache pruning on.
- **Latency is noisy.** Identical runs on a shared laptop differed by up to 3x; treat cold p50/p95 and
  concurrency numbers as orders of magnitude only.

