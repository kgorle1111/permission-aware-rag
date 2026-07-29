# Permission-Aware RAG

**Retrieval-augmented generation that cannot leak documents the caller isn't allowed to see — enforced by construction, proven by an automated leak-rate gate on every commit.**

[![CI](https://github.com/kgorle1111/permission-aware-rag/actions/workflows/ci.yml/badge.svg)](https://github.com/kgorle1111/permission-aware-rag/actions)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![Dependencies: zero](https://img.shields.io/badge/dependencies-zero-brightgreen)
![License](https://img.shields.io/badge/license-Apache--2.0-lightgrey)

A working vertical-AI product, not a toy: an **insurance underwriting workbench** where a
junior underwriter, a senior, a compliance officer, and an auditor ask the same question
and each sees only what their role permits — down to the ranking math. One structured LLM
call drafts cited findings on top; a human always makes the decision.

**Built end-to-end in Python stdlib only.** No vector database, no framework, no
dependencies — every security property is in ~600 lines you can actually read.

---

## The problem

Enterprise RAG has a well-known failure mode: the retrieval index doesn't know about
document permissions. Index everything, and any employee can phrase a query that surfaces
the salary file or the compliance watchlist — through the answer, the citations, or even
the *relevance scores* of documents they're allowed to see.

Most implementations "fix" this by post-filtering: rank everything, then drop forbidden
results. That still leaks — through score shifts, result ordering, and count side channels
tied to specific queries.

## The guarantee

This system makes the leak **structurally impossible** rather than filtered-after-the-fact:

1. **Pre-filtering** — chunks the caller cannot read are removed *before* ranking.
   Forbidden content is never scored, so it cannot influence ordering or citations.
2. **Visible-set statistics** — BM25's df / corpus-size / average-length are computed over
   the caller-visible set only, closing the subtler side channel where a hidden document's
   term frequencies shift the scores of visible ones. (This bug existed in v1 — it was
   found by an adversarial self-review, reproduced with a failing test, fixed, and the
   regression test asserts byte-identical scores with and without a hidden document.)
3. **A leak gate in CI** — a 20-case eval suite asserts, per role, both *expected* documents
   (recall@4: 14/14) and *must-never-return* documents (leak rate: 0). Any leak fails the
   build. Retrieval-quality changes (TF-IDF → BM25, chunking rewrite) merged only after
   this gate passed unchanged.

```
User question ──► ACL pre-filter ──► BM25 over visible set ──► top-k chunks
                     │                                            │
                     ▼                                            ▼
              hash-chained audit log              one structured LLM call (Haiku 4.5)
         (who / what / returned / denied)         grounded-only · citations required
                                                  citations verified post-hoc
                                                            │
                                                            ▼
                                          "Draft findings — verify before acting"
                                              (the human makes the decision)
```

## Try it in 60 seconds

```bash
git clone https://github.com/kgorle1111/permission-aware-rag && cd permission-aware-rag/app
python3 run_evals.py                # the leak gate: 20 cases | recall@4: 14/14 | leaks: 0
python3 underwriter_server.py 8421  # open http://127.0.0.1:8421
```

No install step — stdlib only. Switch roles with keys `1–4` and re-ask the same question:
sources appear and disappear with the role, and the UI explains exactly which data
classes are hidden and who to escalate to. Set `ANTHROPIC_API_KEY` to enable drafted
answers (~$0.002/query on Haiku 4.5; degrades gracefully to retrieval-only without it).

## Engineering highlights

For readers evaluating the engineering rather than the demo:

| Area | What's here |
|---|---|
| **Eval-driven development** | Retrieval changes gate on a per-role eval suite with *negative* assertions (must-not-return docs) — for a permissions product, the absence of a result is the spec. Runs in CI on every push. |
| **Prompt-injection boundary** | Retrieved text is framed in `<document>` tags and declared data-not-instructions; tested by inspecting the actual assembled API payload (mocked transport, zero spend). |
| **Hallucination containment** | Every `[doc-id]` the model cites is verified against the retrieved set; unverified citations are surfaced to the user, not hidden. |
| **Tamper-evident audit** | Each JSONL audit entry chains a SHA-256 of the previous line; `verify_audit_chain()` detects any edited or removed entry. Trail survives restarts. |
| **Cost & latency receipts** | Every LLM answer returns `llm_ms` and `est_cost_usd` from real token usage; running totals per session. Value claims are measured, not estimated. |
| **Production seams** | SSO-ready: one env var switches identity from demo dropdown to HS256 JWT validation (constant-time compare, expiry) — `can_read()` untouched. Rate limiting, input caps, CSP/nosniff, XSS-safe rendering throughout. |
| **Prompt caching** | Static system prompt marked `cache_control: ephemeral`; per-request context deliberately uncached. Token usage surfaced per response to verify cache engagement. |
| **Test discipline** | Four test files: exact-content leak tests, role ACL tests, mocked-LLM payload tests, and HTTP endpoint tests against a real in-process server (auth, rate-limit 429s, CSV export). Plus ruff lint + format gating CI. |
| **Frontend** | Single-file vanilla-JS workbench on a token-based design system (dark + light, WCAG-checked), inline SVG icons, strict CSP with zero external origins. Deep links, keyboard-first, audit trail with CSV export. |

## Threat model (what's handled, what's not)

| Vector | Status |
|---|---|
| Forbidden doc in results/citations | ✅ Impossible — excluded before ranking |
| Score side channel from hidden docs | ✅ Fixed — visible-set statistics; regression-tested |
| Prompt injection via document text | ✅ Bounded — data/instruction framing + payload tests |
| Hallucinated citations | ✅ Detected — post-hoc verification, surfaced in UI |
| Audit log tampering (edit/remove) | ✅ Detected — SHA-256 hash chain |
| Audit log truncation from the tail | ⚠️ Needs an externally anchored head hash — documented, deferred |
| Cross-doc aggregation (LLM synthesizes a conclusion no single doc supports) | ⚠️ Mitigated by citation-required prompting; needs answer-level evals |
| Denied-count side channel | ⚙️ Deliberate demo feature; `SHOW_DENIED=0` disables it |

## API

```python
from permission_rag import PermissionRAG

rag = PermissionRAG(audit_path="audit_log.jsonl")
rag.add_document("salaries", "salary bands range from 90k to 250k", {"group:hr"})

rag.retrieve("salary bands", {"id": "bob", "groups": ["hr"]}, k=3)  # → ranked chunks
rag.retrieve("salary bands", {"id": "alice", "groups": ["eng"]})  # → [] (never scored)

PermissionRAG.verify_audit_chain("audit_log.jsonl")  # → True unless tampered
```

```bash
curl -s -X POST http://127.0.0.1:8421/ask -H 'content-type: application/json' \
  -d '{"user":"senior","q":"can we bind Delgado above 1 million?"}'
# → {"results": [...], "answer": "...", "llm_ms": 840, "est_cost_usd": 0.0019,
#    "unverified_citations": [], "denied_chunks": 1}
```

Full surface: `POST /query` (retrieval only), `POST /ask` (adds the drafted answer,
rate-limited), `GET /audit` (per-caller scoped; `&format=csv` to export), `GET /presets`.
ACL entries are `user:<id>`, `group:<name>`, or `"*"`; empty ACLs and duplicate ingests
are rejected at write time.

## Scope and honest limitations

- **Ranking is BM25, on purpose.** The contribution is the permission model; `_score()` is
  one function to swap for embedding cosine, and the ACL logic doesn't change. The eval
  gate is what makes that swap safe.
- **Synthetic corpus.** Seven documents across four data classes — enough to demonstrate
  and test every property. The production path (ACLs from systems of record, IdP-issued
  JWTs, TLS) is designed and documented, not built.
- **Single-process.** Rate limits and cost totals are in-memory; the audit log is a local
  JSONL. Appropriate for the pilot scale this targets.

## Development process

This repo was built as a disciplined seven-wave cycle and the artifacts are public:
an adversarial security review of the first prototype ([`BACKLOG.md`](BACKLOG.md) — five
findings, two of which broke the product's core claim, all fixed with regression tests),
a product case file with a risk register ([`CASE_FILE.md`](CASE_FILE.md)), and an
integration map for fitting a real underwriting shop
([`INTEGRATION.md`](INTEGRATION.md)). Every wave shipped behind the test suite and the
eval gate; the commit history reads as the changelog.

## License

Apache-2.0
