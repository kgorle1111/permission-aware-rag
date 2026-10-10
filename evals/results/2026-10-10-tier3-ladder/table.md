# Tier 3 retrieval ladder, 2026-10-10

Command: `python3 evals/ladder.py --check` (exit 0). Side measurements: `python3 evals/tier3_extras.py`.
Every rung runs the same gates as the baseline: kit leak suite, role-isolation oracle, hierarchy isolation,
recall@4 on `app/evals.json`. Any leak or isolation diff rejects a rung whatever its quality. The last
nine rows are negative controls and must be REJECTED. The new `SemanticCacheNoScope` (cache key without the
principal, no readability re-check) is one of them, so gate recall is 9/9.

| rung | verdict | leaks | isolation diffs | recall@4 | p50 ms | p95 ms | why rejected |
|---|---|---:|---:|---:|---:|---:|---|
| PermissionRAG (baseline) | accepted | 0 | 0/316 | 14/14 (baseline) | 0.02 | 0.02 | - |
| SemanticCacheRAG | accepted | 0 | 0/316 | 14/14 (+0 vs baseline) | 0.06 | 0.06 | - |
| StructureChunkedRAG | accepted | 0 | 0/316 | 14/14 (+0 vs baseline) | 0.02 | 0.02 | - |
| MultiQueryRRF | accepted | 0 | 0/316 | 14/14 (+0 vs baseline) | 0.05 | 0.07 | - |
| RoutedRAGFake | accepted | 0 | 0/316 | 14/14 (+0 vs baseline) | 0.03 | 0.05 | - |
| AbstainRAG | accepted | 0 | 0/316 | 14/14 (+0 vs baseline) | 0.02 | 0.02 | - |
| NoFilter | REJECTED | 200 | 314/316 | 14/14 (+0 vs baseline) | 0.02 | 0.02 | 200 leaks, 314 isolation diffs |
| GlobalIdf | REJECTED | 124 | 313/316 | 14/14 (+0 vs baseline) | 0.02 | 0.02 | 124 leaks, 313 isolation diffs |
| PostFilterTopK | REJECTED | 85 | 313/316 | 14/14 (+0 vs baseline) | 0.02 | 0.02 | 85 leaks, 313 isolation diffs |
| StarSubstring | REJECTED | 105 | 283/316 | 14/14 (+0 vs baseline) | 0.02 | 0.03 | 105 leaks, 283 isolation diffs |
| GroupPrefix | REJECTED | 39 | 187/316 | 14/14 (+0 vs baseline) | 0.02 | 0.02 | 39 leaks, 187 isolation diffs |
| SharedCache | REJECTED | 146 | 259/316 | 14/14 (+0 vs baseline) | 0.00 | 0.00 | 146 leaks, 259 isolation diffs, 3 suite recall misses |
| AnyLevelGrants | REJECTED | 200 | 314/316 | 14/14 (+0 vs baseline) | 0.02 | 0.03 | 200 leaks, 314 isolation diffs |
| FlattenIntersection | REJECTED | 0 | 3/316 | 14/14 (+0 vs baseline) | 0.02 | 0.02 | 3 isolation diffs, 3 suite recall misses |
| SemanticCacheNoScope | REJECTED | 146 | 259/316 | 14/14 (+0 vs baseline) | 0.15 | 0.16 | 146 leaks, 259 isolation diffs, 3 suite recall misses |

## Side measurements (`evals/tier3_extras.py`)

```
semantic cache: 80 hits / 100 lookups (5 repeats of 20 queries)
embedding cache: 81 hits / 100 embeds
abstain @ 1.0: answered 14/14 answerable; abstained 3/6 no-expected-doc queries
chunks on the demo corpus: baseline 7, structure-aware 7
```

## Reading it honestly

- **No rung beat the baseline on recall.** All score 14/14, as does the baseline: the 20-query, 7-chunk lexical
  corpus saturates, so it cannot show gains from multi-query, rewriting or better chunking. "Equal" means
  "this eval cannot tell", not "no benefit". A harder golden set (ROADMAP tier 1) is the prerequisite for any quality claim.
- **Semantic cache loses on latency here.** p50 0.06 ms vs 0.02 ms: with 7 chunks, BM25 is cheaper than
  embedding (even memoized: sha256 plus 256-float dot products) plus a cache scan. Hit rate was 80/100 on
  repeated queries, but a saving only exists once retrieval costs more than the lookup. At this scale it is a
  correctness-gated mechanism, not a speedup.
- **Embedding cache** removed 81 of 100 embed calls on the repeated workload (a hit-rate statement, not a speed claim).
- **Chunking v2** produced the same 7 chunks as the baseline (documents are one short paragraph). Its guarantees
  (never merges across differing ACL levels, 15% sentence overlap, breadcrumb from section titles) are proven by
  `app/test_rungs.py`, not by this table. Sizes are counted in words, not model tokens (ROADMAP B10).
- **Router / rewrite / HyDE / step-back** used deterministic fakes (`fake_classifier`, `fake_enhancer`: lexical
  rewrites). No LLM was called, so nothing here says anything about real LLM gains. The enhancer receives only
  `(mode, query)`; its output is treated as an untrusted query for the same pre-filtered retriever.
- **Multi-query RRF** uses deterministic lexical variants, k=60, one audit entry per user query.
- **Abstain at 1.0** (chosen before measuring, not tuned): answered 14/14 answerable queries and abstained on
  3 of 6 queries with no expected document. The other 3 still scored >= 1.0 on some visible chunk, the usual
  precision limit of a score threshold on BM25 (scores are not comparable across queries). A real threshold
  needs calibration on a labeled unanswerable set.
- **Oracle change.** Rungs whose scores differ from BM25 by design (RRF, merged router output, abstain,
  chunking) set `SCORES_DIFFER`; `run_evals._oracle` then compares the rung against the same class built over
  only the role's readable docs (ids and scores must match exactly). The semantic cache keeps the stricter
  baseline `PermissionRAG` oracle. This is weaker than the baseline oracle for those rungs (a scoring bug shared
  by both sides would not show as a diff); the kit's visibility suite and label gate still apply to them.
- Latency is 5 repeats of 20 queries on a 7-chunk in-memory corpus: a smoke number, not a benchmark.
