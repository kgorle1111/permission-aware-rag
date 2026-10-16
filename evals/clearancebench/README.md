# ClearanceBench eval: direct and existence leaks on a public benchmark we did not write

[ClearanceBench](https://huggingface.co/datasets/NagaYu/clearance-bench) is a public, synthetic leak
benchmark from an independent author ([generator](https://github.com/NagaYu/clearance)). It has 4,000 documents,
14,067 chunks and 600 principals, with overlapping ACLs, clearance levels, embargo windows and grants on a
timeline. As of 2026-10-16 no named system had published results on it.

```bash
python3 evals/clearancebench/fetch.py      # pinned revision, sha256-checked, into ./data/ (gitignored)
python3 evals/clearancebench/run.py        # prints the table; --write saves it under evals/results/
```

## Results (2026-10-16)

[table.md](../results/2026-10-16-clearancebench/table.md), every number in
[report.json](../results/2026-10-16-clearancebench/report.json).

- **Direct leaks:** 0 of 38,400 result slots (3,840 queries x top 10; Wilson 95% upper 0.01%), 0 canary hits.
- **Existence:** 0 of 20 score-identity samples differ.
- **Revocation:** 50 of 50 cases clean after removing the token.
- **Controls:** NoFilter leaks on 328/328 probe queries. Read-up and embargo bugs each leak on 400/400 targeted
  probes, against 0/400 for the real engine on the same probes.
- **Oracle vs the generator:** the oracle reproduces `n_chunks_lost` on 50/50 revocation cases. It agrees that
  the probe target is unreadable for its asker on 326/328 (probe, time) pairs. The 2 exceptions are one target
  (`D03003#0`) whose embargo window [100, 800) is open at t = 250 and 500, where its asker holds a matching
  token and enough clearance. The read is legitimate at those times, so it is correctly not a leak.
- **Recall, the weak number:** exact-chunk probe recall by a legitimate reader is hit@10 59.7%. Same-document
  recall is hit@10 97.5%. Templated text makes sibling chunks near-duplicates for BM25.

## Source and licence (verified 2026-10-16)

Revision `0ff4169c80850ccce5fe7eb95efd24aa9516e8a2`, Apache-2.0 per the dataset card, fully synthetic: no real
documents, ACLs or identities. The six parquet files are pinned by size and by the LFS sha256 Hugging Face
publishes ([MANIFEST.json](MANIFEST.json)).

## Mapping (exact, tested)

The benchmark's rule: a principal may read a chunk when **all** hold:
1. it holds one of the chunk's `allow_tokens` (OR)
2. `level >= min_level`
3. `not_before <= t < not_after`
4. the capability is held at time `t`

Each benchmark chunk is ingested as one engine document with one chunk, and the engine's three ACL levels
(AND across levels) carry the rules:

| Engine level | Holds | Principal gets |
|---|---|---|
| document | `group:tok:<token>` for each allow token | `tok:<token>` for each token held at `t` |
| section | `group:clearance:<min_level>` | `clearance:0` … `clearance:<level>` |
| paragraph | `group:open@<t>` for each evaluation time inside the window (else `group:never-open`) | `open@<t>` |

`app/test_clearancebench_eval.py` checks, for every combination on a small grid, that the engine's own check
equals the benchmark rule at every evaluation time. Two planted mapping bugs (off-by-one clearance,
inclusive window end) turn it red.

## What is measured

- **Direct leaks.** Every query, at each evaluation time `t ∈ {0, 250, 500, 1000}` (fixed before the first
  run), asked by its own asker and by 3 random other principals; top 10. A leak is a returned chunk the
  principal may not read. It is detected two independent ways: by id against an oracle that re-implements
  the four rules from the benchmark tables (not the engine's check), and by scanning returned text for the
  benchmark's canary markers.
- **Existence (score identity).** For 20 sampled (query, asker, time) triples, the results over the full index
  must equal, ids and scores, the results over an index holding only that principal's readable chunks.
- **Revocation.** For each of the 50 `revocation_cases`: at the first evaluation time where the target is
  readable, retrieve; remove the token; the target must disappear and nothing unreadable may appear.
- **The oracle against the generator's own ground truth.** Every probe target must be unreadable for its asker,
  and the oracle must reproduce each revocation case's `n_chunks_lost`.
- **Controls.** `NoFilter` (no ACL check), a read-up bug (clearance ignored) and an embargo bug (window ignored)
  must all leak; the read-up and embargo bugs get targeted probes (a chunk's own first 12 words, asked by a
  principal only that check would deny), and the same probes run on the real engine.

## Limits (read before quoting a number)

- **Synthetic, templated text.** It complements the real-mail [EnronQA eval](../enronqa/README.md); it does
  not replace it. BM25 struggles to separate near-identical templated chunks, so chunk-level recall is low.
- **No relevance labels for normal queries.** Recall is reported only as probe-target retrieval by a principal
  who may read the target, at chunk level and at document level. The document-level metric was added after
  the first smoke run, and it is labelled so in the table.
- **Time is enforced through the mapping, not by an engine clock.** The engine has no notion of time; the
  harness turns the window into a group at each fixed evaluation time.
- **Grants do not expire here.** `access_events` lists grant times but no durations, so expiring grants are not
  tested; the oracle and the engine use the same effective token set.
- **Revocation is instant by construction in this engine.** Permissions arrive with each query, so the test
  shows the result set is correct after removal, not propagation lag. The platform's sync path is measured
  elsewhere.
- **Derivative leaks** (summaries, agent memory, caches surviving a revocation), the benchmark's headline
  channel, are not applicable: the reference engine keeps none of them. They are not claimed.
- **The hidden-chunk count** is not exposed by default since T11 (`SHOW_DENIED`), so it is not an existence
  channel here; the audit log still records it.
