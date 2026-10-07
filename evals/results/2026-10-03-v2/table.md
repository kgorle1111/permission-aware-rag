# Scaled leak eval — 2026-10-03

Produced by `app/eval_scale.py` on a frozen eval set (`sha256 db6b5dcb51a5…`): 210 docs
(30 synthetic accounts × 7 kinds, each kind behind a different ACL, including trap ACLs
like `group:banking*`), 630 probe phrasings × 4 roles. Intervals are Wilson 95%.

| backend | docs | leaks / must-not probes | leak rate 95% UB | recall@4 | recall 95% CI | eval set |
|---|---|---|---|---|---|---|
| in-memory BM25 | 210 | 0/1350 | 0.28% | 1170/1170 = 100.0% | 99.7%–100.0% | `db6b5dcb51a5` |
| pgvector + RLS | 210 | 0/1350 | 0.28% | 1167/1170 = 99.7% | 99.2%–99.9% | `db6b5dcb51a5` |

## Pre-registered claims ([ROADMAP](../../../ROADMAP.md#pre-registered-claims))

- **E1, zero leaks at scale: shown for in-memory.** 0/1350 at n ≥ 1,000, with a UB of 0.28% (< 0.5%).
- **E3, pgvector matches: shown.** It has 0 leaks on the same frozen set, under Postgres RLS (CI run
  37270074256, `pgvector/pgvector:pg16`). It misses 3 recall probes because of the feature-hash embedder.
- **E4, recall useful: shown, but weakly.** The lower bound is 99.7% (≥ 0.70). Every probe is
  drawn from the target's own text (full text, title, last 8 words), so this measures
  lexical recall, not semantic recall.

## Does this eval fail when it should?

The same frozen set, run against the known-leaky retrievers in `app/mutants.py`:

| mutant | leaks / 1350 |
|---|---|
| NoFilter | 1350 |
| StarSubstring | 686 |
| GroupPrefix | 732 |
| SharedCache | 270 |
| GlobalIdf | 0 |
| PostFilterTopK | 0 |

It catches 4/6. The two misses leak through **scores**, not doc ids, and a doc-id leak count
can't see that by construction. `run_evals.py`'s isolation gate catches both (6/6).
The two evals are complements: this one bounds the rate, and the isolation gate covers
the side channels.

## Caveats

- The probes aren't independent. They share 30 accounts and one retriever, so treat the
  bound as a bound on *this distribution*, not on arbitrary corpora.
- The corpus is synthetic and generated from templates. A real-corpus held-out run (ROADMAP Next 8) is still open.
