# Case File: Permission-Aware Underwriting Assistant

**Date:** 2026-07-20 · **Status:** Prototype (local, synthetic data) · **Owner:** Kannishk

## The use case

Internal tool for insurance/lending underwriters. An underwriter asks questions in plain
English ("can we bind Delgado above $1M?"); the system retrieves only the documents that
underwriter's role permits — policy status, claims history, bank profiles, credit memos,
compliance watchlists — and one structured LLM call drafts findings with citations.
**The AI drafts; the underwriter decides.** No approve/deny language, ever.

Why permission-aware retrieval is the whole product: underwriting shops mix data classes
with different access rules (claims vs. banking vs. compliance flags). A naive vector
index over all of it leaks — a junior can phrase a query that surfaces the compliance
watchlist through scores or citations. Here denied chunks are excluded **before ranking**,
so forbidden content can't influence anything the user sees.

## Roles → data classes (current synthetic corpus)

| Role | underwriting | banking | senior | compliance | public guidelines |
|---|---|---|---|---|---|
| junior | ✅ | — | — | — | ✅ |
| senior | ✅ | ✅ | ✅ | — | ✅ |
| compliance | ✅ | — | — | ✅ | ✅ |
| auditor | — | ✅ | — | ✅ | ✅ |

Enforced by `PermissionRAG.can_read` at query time; tested in `app/test_underwriter.py`
including exact-content leak queries.

## Architecture (house pattern)

Deterministic guards (ACL pre-filter, empty-ACL refusal at ingest) → one structured LLM
call (`app/llm.py`, grounded-only system prompt, citations required, "insufficient
context" forced phrasing) → human-review-always-wins (findings language, never
decisions) → audit trail (every retrieval logs who/what/returned/denied-count).

## Prompt caching design

- **Cached:** the static system prompt (underwriting guidelines + rules) via
  `cache_control: ephemeral`. This is the stable prefix every request shares.
- **Not cached:** retrieved context + question — they change per request, caching them
  would just churn cache writes.
- **Caveat:** Haiku 4.5's minimum cacheable block is 4096 tokens; the current stub prompt is
  below that, so cache reads show 0 until the real guidelines manual is pasted in.
  `usage` (input / cache_read / output tokens) is surfaced in every response so you can
  verify the moment it engages.
- **Cost:** Haiku 4.5, ~1–2k tokens in / ~300 out per question → well under $0.01/query;
  cached guidelines cut the input side ~90% once the manual is real.

## What's synthetic vs. what's real for production

Real corpus would come from policy admin systems, claims platforms, core banking, and
compliance case tools. Before any real data: (1) identity from SSO, not a dropdown;
(2) ACLs sourced from the systems of record, not hand-written; (3) TLS + authn on the
HTTP surface; (4) persistent audit log; (5) eval set of ~20 hand-labeled Q/A pairs per
role before any prompt iteration.

## Risks / open questions

- **Aggregation risk:** a senior sees banking + credit docs individually fine, but the
  LLM synthesizing across them may produce conclusions no single doc supports. Mitigated
  by citation-required prompting; needs eval coverage.
- **IDF side channel (FIXED 2026-07-28):** df/n are now computed over the
  caller-visible set at query time, so hidden docs cannot shift visible scores.
  Regression-tested in `test_permission_rag.py`. Was BACKLOG.md S1.
- **"N chunks hidden" side channel:** the denied count is shown in the UI. Deliberate for
  the demo (it sells the feature); consider hiding per-query counts in production.
- **BM25 ceiling:** exact-word matching only; swap `_score` for embedding cosine when
  recall matters. ACL logic is unchanged by that swap.
- **Regulatory:** if this touches real consumer credit decisions, ECOA/FCRA adverse-action
  territory — one more reason output stays "findings for human review."

## Next steps

1. Paste a real (or realistic ~4.5k-token) guidelines manual into `SYSTEM_PROMPT` →
   verify cache reads (Haiku 4.5 minimum cacheable block is 4096 tokens).
2. ~~Build the 20-case eval harness~~ Done — `app/run_evals.py`, gates CI.
3. Value receipts: per-query `llm_ms` + `est_cost_usd` now ship in every /ask and roll
   up in `/audit.llm_summary`; add a time-saved estimate for the pilot pitch.
