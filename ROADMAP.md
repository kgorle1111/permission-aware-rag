# Roadmap

What's shipped, what's next, and what we're deliberately not building. Every claim here
links to a test, a committed result, or a ledger row:
[decisions](docs/DECISIONS.md) · [threat model](docs/THREAT_MODEL.md) ·
[eval results](evals/results/2026-10-03-v2/table.md).

PRs [#6](https://github.com/kgorle1111/permission-aware-rag/pull/6) through [#12](https://github.com/kgorle1111/permission-aware-rag/pull/12) are merged. The AWS reference demo now runs deployment 2 from merged commit `b65e0c4`; [release verification](evals/results/2026-10-10-aws-release/table.md) records the live permissions/privacy checks and scope.

## Shipped and implemented

| When | What | Evidence |
|---|---|---|
| 2026-07-28 | Seven improvement waves: BM25, sentence chunking, hash-chained audit, JWT seam, rate limits, injection boundary, citation verification, workbench UI | CI on every push |
| 2026-10-03 | Leak gate made falsifiable: 6 deliberately leaky retrievers plus a label-free isolation check. The old hand-labeled gate caught 1/6; the combined gate catches 6/6 | `app/mutants.py`, `app/run_evals.py` |
| 2026-10-03 | Scaled leak eval on a frozen 210-doc set: **0/1,350 leaks (95% upper bound 0.28%)**, in-memory and pgvector | [results](evals/results/2026-10-03-v2/table.md) |
| 2026-10-03 | Decisions, threat model and shortcut ledgers, each enforced by tests | `app/test_ledgers.py` |
| 2026-10-05 | Production service consolidated into [`platform/`](platform/README.md) with its history: FastAPI, Qdrant, RS256 JWT, Google Drive permission sync | `.github/workflows/platform.yml` |
| 2026-10-05 | Platform mutation gate made blocking: 61 surviving mutants killed by new tests, 15 pinned as reviewed equivalents | `platform/mutation_equivalents.json` |
| 2026-10-07 | Independent document/section/paragraph ACL levels in memory, PostgreSQL RLS and Qdrant; isolation oracle catches both hierarchy mutants (8/8 total) | `app/test_hierarchy.py`, `app/test_pgvector.py`, `platform/tests/test_hierarchy.py` |
| 2026-10-07 | Qdrant keyword indexes, document-filtered batched ACL updates, on-disk payload/vectors and int8 configuration; durable retry barriers | `platform/tests/test_scale_acl.py`, `platform/tests/test_hardening.py`, `evals/verify_qdrant_storage.py` |
| 2026-10-07 | AWS Lightsail HTTPS portfolio demo, with synthetic documents and retrieval-only roles | [deployment guide](deploy/aws/README.md) |
| 2026-10-10 | Measurement foundation: answer metrics (7.2, offline judge only), ladder harness that rejects any leaking rung and rejects all 8 mutants (7.4), and `run_evals.py --mutants` plus `ladder.py --check` in CI (1.4) | `evals/metrics.py`, `evals/ladder.py`, `app/test_answer_metrics.py`, `app/test_ladder.py` |
| 2026-10-10 | Tier 3 retrieval rungs, each measured and each passing every leak gate: scoped semantic cache (8.5), embedding cache (8.6), structure-aware chunking (9.2), router + rewrite/HyDE/step-back (9.3, deterministic fakes only), multi-query RRF (9.4), abstain threshold (9.7). The unscoped-cache mutant is caught (9/9). No rung beat the baseline on recall; the lexical corpus cannot show it either way | [ladder table](evals/results/2026-10-10-tier3-ladder/table.md), `app/rungs.py`, `app/test_rungs.py` |


### Evidence batch — 2026-10-08

The public reports, kit and walkthrough were merged through [PR #7](https://github.com/kgorle1111/permission-aware-rag/pull/7) and subsequent evidence PRs. The article is kept privately at the maintainer’s request.

- [100k-document / 1.5M-chunk local measurement](evals/results/2026-10-08-scale/table.md): filtered engine retrieval met the local p95 target; actual folder reconciliation took **104.92 s**, missing the <60 s target. Production full-materialization ingest and end-to-end API timing remain unmeasured.
- [Public-input fixture and adapter evidence](evals/results/2026-10-08-public-kit/table.md): 87 National Archives paragraphs with fictional permissions; zero failures in 2,088 visibility/isolation probes, 197/197 required hits, and eight planted faults caught. The missing historical corpus remains unrecovered.
- [Portable leak-test kit](evals/kit/README.md) with an independent ACL oracle, positive controls, reference/standalone adapters and an optional promptfoo provider. [Actual CLI integration](evals/results/2026-10-08-promptfoo/table.md) passed three local positive cases and rejected two deliberate negative cases. An external unassisted integration remains unproven.
- Article draft kept privately in gitignored marketing materials at the maintainer’s request. The [five-minute walkthrough](docs/WALKTHROUGH.md) is drafted; publication and rehearsal are not claimed.

### First adversarial security review (2026-07-20)

| # | Severity | Finding | Status |
|---|---|---|---|
| S1 | High | IDF side channel: hidden docs shifted visible BM25 scores | Fixed 2026-07-28: statistics over the caller-visible set; score-identity regression test |
| S2 | High | `/audit` let any role read every user's queries | Fixed 2026-07-28: scoped to the caller; full view only for the audit group |
| S3 | Medium | Prompt injection: document text spliced into the prompt with no boundary | Fixed 2026-07-28: `<document>` data boundary + payload test. Closing-tag escaping fixed 2026-10-07 (`app/test_security.py`) |
| S4 | Low | Demo page rendered document text via `innerHTML` | Fixed 2026-07-28: escaped, plus CSP/nosniff headers |
| S5 | Low | `/ask` was an unthrottled paid API call | Fixed 2026-07-28: per-IP rate limit |

## Pre-registered claims

These were written before the scaled eval ran. Negative results get published the same way.

| Id | Claim | Not shown if | Status |
|---|---|---|---|
| E1 | Pre-filtering has zero leaks at scale | any leak, or upper bound > 0.5% at n ≥ 1,000 | **shown**: 0/1,350, UB 0.28% |
| E2 | The leak gate detects realistic leak bugs | fewer than 100% of the known-leaky retrievers caught | **shown**: 8/8 after hierarchy probes |
| E3 | pgvector + RLS matches in-memory on leaks | any leak on either backend | **shown**: 0/1,350 on both |
| E4 | Recall is useful, not just safe | recall@4 lower bound < 0.70 on the frozen set | shown, but weak: probes are lexical |

### Completed security regressions

**Security regressions:** document text/id breakout, CSV formulas, newest-entry edits, citation ids and malformed field types are pinned by `app/test_security.py`. List bodies already returned 400 on both endpoints; explicit validation also works with Python assertions disabled. Auditors receive other users' query text redacted. Local JSONL tail checkpoints and their storage-attacker limits are documented in [T12](docs/THREAT_MODEL.md).
### Independent verification — 2026-10-10

A fresh contract author and reviewer found and repaired audit query persistence,
platform cross-user audit exposure and missing platform document prompt boundaries.
An inconsistent rebuild-state probe also led to defensive denial hardening.
[Authorship, preserved failures and current verification](docs/INDEPENDENT_VERIFICATION.md)
distinguish before-fix checks from after-fix regressions. Current local platform:
387 tests, 98.97% coverage; container verification and the mutation gate passed
(1,688/1,708 killed, 20 equivalents).
[PR #12](https://github.com/kgorle1111/permission-aware-rag/pull/12) is merged after both complete Linux mutation campaigns passed. The current reference image is live on AWS deployment 2; [release checks](evals/results/2026-10-10-aws-release/table.md) passed.

## Next

1. **External integration:** have an engineer run the kit unassisted against a separate retriever (L4). [Run protocol and feedback form](docs/EXTERNAL_VALIDATION.md) are prepared; no external result is claimed.
2. **Rehearse the interview walkthrough.** [Timed rubric](docs/PUBLICATION_CHECKLIST.md) is prepared; actual human rehearsal remains open. Article publication stays paused.
3. **Broader real-world eval — paused:** resume only with industry-relevant input and documented non-fictional permissions. [Source research and requirements](docs/REAL_INPUT_REQUIREMENTS.md) record why public regulations plus invented roles do not qualify.

Completed integration: PRs #6–#12 are merged and the updated AWS reference demo
passed live release checks. Durable document intents reduced affected-row
hydration/vector replay; [remeasurement](evals/results/2026-10-08-sync-optimized/table.md)
remains single-document 0.63 s and folder 13.31 s at its pinned historical source.
Global SQL integrity scans remain. Pending pre-journal platform indexes require
full reingestion before retrieval resumes; clean upgrades retain readiness.

## Permissions that scale to 100,000 documents

**The target:** ~100k documents, roughly 1.5M chunks, on one vector-database node.

- **Permissions are authored as a hierarchy:** document → section → optional paragraph. Each level can only narrow the one above.
- **Every chunk carries one principal list per level.** Search checks all levels inside the vector database's filtered search.
- **Query cost depends on how many groups the user is in, not how many documents they can read.**
- **Revoking a document is one bulk update**, not one write per chunk. Adding someone to a group touches no index rows at all.
- **The flat ACL merge is replaced.** It wrongly locked out users who qualified through different groups at different levels.
- **Prerequisite fixes:** payload indexes on the permission fields, batched permission updates, and vector quantization.
- **Hybrid search must never use collection-wide IDF.** It would reopen the score side channel that S1 closed.

Hierarchy and the Qdrant configuration fixes are implemented. The remote-server diagnostic verifies storage settings, engine filtering and bulk revocation; it does not measure resident memory or large-scale latency. Permission reconciliation still scans the SQL mirror. The first [100k-document benchmark](evals/results/2026-10-08-scale/table.md) measured engine search and actual reconciliation: warm local search met the latency target, while folder reconciliation missed its target. Production ingest, concurrency and API latency remain unmeasured. The goals: retrieval p95 under 100 ms at every selectivity level, a 10k-document folder revocation in under 60 seconds, and zero leaks. Misses get published.

## Scoped, not scheduled

Designed and scoped on purpose, but not scheduled. Each waits for a reason to build it: a measured gap, a reviewer's question, or real use.

- **Generation hardening:** repeat the data-not-instructions rule after the documents, and pin the model to a dated version instead of an alias.
- **Embedding fingerprint:** the index records which embedding model built it, and the service refuses to query with a different one (prevents silent mismatches).
- **PII redaction** at ingest and on output.
- **Spend cap** per day, plus a red-team suite of 50+ injection and exfiltration attacks in CI.
- **Request logs for latency, cost and retrieval failures:** one structured line per request (request id, outcome, per-stage latency, tokens, estimated cost, failure reason) in both apps, with p50/p95 latency, daily cost and failure rate in `/audit`. Logs hold ids and counts only, never query or document text.
- **Model SDK contract test** against the real API in CI.
- **Bind principals server-side** ([T18](docs/THREAT_MODEL.md)), so SQL on the app connection can't forge `rag.principals`.

### The full RAG build-out, one measured rung at a time

Planned for `platform/`. Each step is a rung on a ladder. It's measured on a golden set before and
after, and kept only if it moves the metric it targets. **Every rung must also pass the same leak
gates as today** (the isolation check and the 1,350-probe eval), because a faster or smarter
retriever that leaks is a regression. The comparison table gets published, including the rungs that lose.

1. **Measurement first** (metrics and ladder harness shipped; still open: golden set, judge calibration, CI regression gate on answer metrics, tracing): a ≥100-question golden set per role, with unanswerable and cross-role
   trap questions; faithfulness, relevancy and context precision/recall; an LLM judge calibrated
   against human labels; a CI gate on regressions; per-stage tracing.
2. **Production patterns:** pinned models with fallback; backoff with jitter; a 4-level
   graceful-degradation chain whose every level stays permission-filtered; a response envelope
   with request id, latency, cost and degraded flag. (Embedding cache: shipped, see the Tier 3 row above.)
3. **Async pipeline:** only when the request logs show load. (Scoped semantic cache and its planted
   "unscoped cache" bug: shipped in the reference app, see the Tier 3 row above; not yet in `platform/`.)
4. **Retrieval ladder:** a real embedding model; hybrid dense + sparse search over visible rows only;
   a local cross-encoder reranker. (Structure-aware chunking, router with rewrite/HyDE/step-back,
   multi-query RRF and the abstain threshold are shipped in the reference app with deterministic
   fakes for LLM steps; see the Tier 3 row above. None is in `platform/` yet.)
5. **Agents:** router → iterative → tool-calling retrieval, with the caller's identity bound
   server-side so a model can't widen its own access, plus turn, token and time caps. A guarded
   multi-agent pipeline only if a single agent measurably falls short.
6. **Beyond text:** PDF tables and OCR, selective chart descriptions, and text-to-SQL under
   row-level security for policy-system data, after principal binding ([T18](docs/THREAT_MODEL.md)); the ingest-role fix (T13) is done.

## Open shortcuts (test-enforced)

Every deliberate shortcut in code (a `kn:` or `ponytail:` comment) must appear here with
its upgrade trigger. `app/test_ledgers.py` fails if a shortcut comment has no row.

| Id | Status | Item | Trigger to build | Where |
|---|---|---|---|---|
| B01 | open | In-memory BM25 index, linear scan per query | Corpus past ~50k chunks, or p95 retrieve > 200 ms | `app/permission_rag.py` "in-memory BM25 ranking" |
| B02 | open | Feature-hash embedder in place of a real model | E4 (recall lower bound ≥ 0.70) fails on non-lexical probes | `app/embedding.py` "placeholder embedder" |
| B03 | open | Rate limiter is in-memory, per process | More than one server process or host | `app/underwriter_server.py` "in-memory per-process" |
| B04 | open | Smallest model tier (Haiku) for grounded answers | An answer-quality eval shows Haiku below bar | `app/llm.py` "smallest tier" |
| B05 | done (T13, 2026-10-10) | RLS ingest gated by a forgeable GUC, not a DB role | — | `app/pgvector_rag.py` "Ingest runs as a separate database role" |
| B06 | done (T14, 2026-10-10) | JWT group with a comma → 500 on pgvector | First real IdP integration (THREAT_MODEL T14) | `app/pgvector_rag.py` "contain no comma" |
| B08 | open | Semantic cache is per process, in memory | More than one server process or host | `app/rungs.py` "per-process cache" |
| B09 | open | Semantic cache scans a scope's entries linearly | Cache holds more than ~10k entries | `app/rungs.py` "linear scan over entries" |
| B10 | open | Chunk size counts words, not model tokens | A real embedder with a hard token limit replaces the feature-hash one | `app/rungs.py` "words approximate tokens" |
| B07 | open | Held-out real-corpus leak eval | Real corpus available | `evals/results/2026-10-03-v2/table.md` "real-corpus held-out run" |

## Deliberately not building

- **GraphRAG:** not until relationship questions ("how are these parties connected?") are a proven need.
- **Hosted multi-tenant service:** not until a real deployment needs it.
