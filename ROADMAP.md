# Roadmap

What's shipped, what's next, and what we're deliberately not building. Every claim here
links to a test, a committed result, or a ledger row:
[decisions](docs/DECISIONS.md) · [threat model](docs/THREAT_MODEL.md) ·
[eval results](evals/results/2026-10-03-v2/table.md).

Current implementation work is in [PR #6](https://github.com/kgorle1111/permission-aware-rag/pull/6), open with passed CI and awaiting review. The 2026-10-07 rows below describe locally verified work on that branch; they are not merged yet. The AWS demo is already live on the prior stable reference image.

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

### Evidence batch — 2026-10-08

The reports, kit and narrative drafts are in [draft PR #7](https://github.com/kgorle1111/permission-aware-rag/pull/7), stacked on PR #6. They are not merged.

- [100k-document / 1.5M-chunk local measurement](evals/results/2026-10-08-scale/table.md): filtered engine retrieval met the local p95 target; actual folder reconciliation took **104.92 s**, missing the <60 s target. Production full-materialization ingest and end-to-end API timing remain unmeasured.
- [Public-input fixture and adapter evidence](evals/results/2026-10-08-public-kit/table.md): 87 National Archives paragraphs with fictional permissions; zero failures in 2,088 visibility/isolation probes, 197/197 required hits, and eight planted faults caught. The missing historical corpus remains unrecovered.
- [Portable leak-test kit](evals/kit/README.md) with an independent ACL oracle, positive controls, reference/standalone adapters and an optional promptfoo shim. An external unassisted integration remains unproven.
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
## Next

1. **Review PR #6**, whose CI checks passed, and the subsequent evidence batch. The live demo still runs the prior stable image.
2. **Review [PR #8](https://github.com/kgorle1111/permission-aware-rag/pull/8), the reconciliation improvement:** durable document intents reduce affected-row hydration and vector replay while preserving retry and deletion barriers. [Remeasurement](evals/results/2026-10-08-sync-optimized/table.md): single-document 0.63 s, folder 13.31 s; necessary global SQL integrity scans remain. Local gates: 358 tests, 98.97% branch coverage, 1,646/1,661 mutants killed and 15 reviewed equivalents.
3. **Broader real-world eval — paused:** resume only with industry-relevant input and documented non-fictional permissions. [Source research and requirements](docs/REAL_INPUT_REQUIREMENTS.md) record why public regulations plus invented roles do not qualify.
4. **External integration:** have an engineer run the kit unassisted against a separate retriever (L4). [Run protocol and feedback form](docs/EXTERNAL_VALIDATION.md) are prepared; no external result is claimed.
5. **Article publication paused; rehearse the interview walkthrough.** [Publication metadata and timed rubric](docs/PUBLICATION_CHECKLIST.md) are prepared; actual publication and human rehearsal remain open.

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
- **Ingest through a separate database role** ([T13](docs/THREAT_MODEL.md)), so the app role can't write chunks.

### The full RAG build-out, one measured rung at a time

Planned for `platform/`. Each step is a rung on a ladder. It's measured on a golden set before and
after, and kept only if it moves the metric it targets. **Every rung must also pass the same leak
gates as today** (the isolation check and the 1,350-probe eval), because a faster or smarter
retriever that leaks is a regression. The comparison table gets published, including the rungs that lose.

1. **Measurement first:** a ≥100-question golden set per role, with unanswerable and cross-role
   trap questions; faithfulness, relevancy and context precision/recall; an LLM judge calibrated
   against human labels; a CI gate on regressions; per-stage tracing.
2. **Production patterns:** pinned models with fallback; backoff with jitter; a 4-level
   graceful-degradation chain whose every level stays permission-filtered; a response envelope
   with request id, latency, cost and degraded flag; an embedding cache.
3. **Async pipeline and semantic cache:** only when the request logs show load or repeat
   queries. The cache is keyed by permission scope and revision, so a cached answer can never
   reach a user with different access. A planted "unscoped cache" bug must be caught first.
4. **Retrieval ladder:** a real embedding model; structure-aware chunking that never merges
   sections with different permissions; query rewriting / HyDE / step-back behind a router;
   multi-query with reciprocal rank fusion; hybrid dense + sparse search over visible rows only;
   a local cross-encoder reranker; an abstain threshold for unanswerable questions.
5. **Agents:** router → iterative → tool-calling retrieval, with the caller's identity bound
   server-side so a model can't widen its own access, plus turn, token and time caps. A guarded
   multi-agent pipeline only if a single agent measurably falls short.
6. **Beyond text:** PDF tables and OCR, selective chart descriptions, and text-to-SQL under
   row-level security for policy-system data, after the database-role fix ([T13](docs/THREAT_MODEL.md)).

## Open shortcuts (test-enforced)

Every deliberate shortcut in code (a `kn:` or `ponytail:` comment) must appear here with
its upgrade trigger. `app/test_ledgers.py` fails if a shortcut comment has no row.

| Id | Status | Item | Trigger to build | Where |
|---|---|---|---|---|
| B01 | open | In-memory BM25 index, linear scan per query | Corpus past ~50k chunks, or p95 retrieve > 200 ms | `app/permission_rag.py` "in-memory BM25 ranking" |
| B02 | open | Feature-hash embedder in place of a real model | E4 (recall lower bound ≥ 0.70) fails on non-lexical probes | `app/embedding.py` "placeholder embedder" |
| B03 | open | Rate limiter is in-memory, per process | More than one server process or host | `app/underwriter_server.py` "in-memory per-process" |
| B04 | open | Smallest model tier (Haiku) for grounded answers | An answer-quality eval shows Haiku below bar | `app/llm.py` "smallest tier" |
| B05 | open | RLS ingest gated by a forgeable GUC, not a DB role | Before any deployment that runs untrusted SQL paths (THREAT_MODEL T13) | `app/pgvector_rag.py` "rag.mode" |
| B06 | open | JWT group with a comma → 500 on pgvector | First real IdP integration (THREAT_MODEL T14) | `app/pgvector_rag.py` "contain no comma" |
| B07 | open | Held-out real-corpus leak eval | Real corpus available | `evals/results/2026-10-03-v2/table.md` "real-corpus held-out run" |

## Deliberately not building

- **GraphRAG:** not until relationship questions ("how are these parties connected?") are a proven need.
- **Hosted multi-tenant service:** not until a real deployment needs it.
