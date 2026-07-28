# Permission-Aware RAG

RAG retrieval that enforces per-user document ACLs **at query time**. The core security
property: chunks the caller cannot read are excluded **before** ranking (pre-filtering),
so denied content can never influence scores, ordering, or citations — the classic
enterprise-RAG leak ("the vector index ignores document permissions") is impossible
by construction.

Zero dependencies — Python standard library only.

## Architecture

```
app/
├── permission_rag.py     # core: ACL pre-filter → TF-IDF rank → audit log
├── llm.py                # one Claude call (Haiku, urllib), prompt caching on system prompt
├── underwriter_server.py # product server: roles, corpus, GET /query /ask /audit, serves ui.html
├── ui.html               # workbench UI (vanilla JS, Trust & Authority design system)
├── demo_server.py        # generic 4-user demo (self-contained page)
└── test_*.py             # leak tests + role ACL tests
```

Flow: deterministic guards (ACL pre-filter, empty-ACL refusal) → one structured LLM
call (grounded-only, citations required; skipped gracefully without an API key) →
human-review-always-wins (findings language, never decisions) → audit trail.
Docs: `CASE_FILE.md` (use case, caching design, risks) · `INTEGRATION.md`
(identity-in / documents-in / answers-out seams) · `BACKLOG.md` (verified 50-item
improvement backlog + security review findings from the 2026-07-20 sweep).

> **Known issue (open, high):** IDF and corpus size are computed over the full corpus,
> so ACL-hidden documents measurably shift visible scores — a side channel that
> contradicts the pre-filtering claim below until fixed. See BACKLOG.md finding S1.

## What it does

- Ingest documents with an ACL: `user:<id>`, `group:<name>`, or `"*"` (public)
- Retrieve with a user identity → only chunks that user may read are scored and returned
- Every retrieval is written to an audit log (who asked what, what was returned, how many chunks were hidden)
- Empty ACLs are rejected at ingest — no ambiguous documents

## How to use

**Pinokio:** click **Start Underwriter Workbench** (or **Start Generic Demo**), then
**Open**. Pick a role, type a query, and watch the same question return different
results per role.

**Underwriter Workbench** (`app/underwriter_server.py`) is the product use case: roles
(junior / senior / compliance / auditor) over insurance + banking docs, plus a `/ask`
endpoint that drafts a cited answer with one Claude call (prompt caching on the static
guidelines system prompt). Set `ANTHROPIC_API_KEY` to enable answers; without it the
endpoint degrades to retrieval-only. See `CASE_FILE.md` for the full use-case write-up.

**Manual:**

```bash
cd app
python3 test_permission_rag.py     # leakage test suite
python3 test_underwriter.py        # role/ACL tests for the underwriter corpus
python3 demo_server.py 8420        # generic demo UI
python3 underwriter_server.py 8421 # underwriter workbench (GET /ask?user=<role>&q=<q>)
```

## API

### Python (library)

```python
from permission_rag import PermissionRAG

rag = PermissionRAG()
rag.add_document("salaries", "salary bands range from 90k to 250k", {"group:hr"})

hr_user = {"id": "bob", "groups": ["hr"]}
results = rag.retrieve("salary bands", hr_user, k=3)
# [{"id": "salaries#0", "doc_id": "salaries", "text": ..., "score": 0.27}]

engineer = {"id": "alice", "groups": ["eng"]}
rag.retrieve("salary bands", engineer)  # [] — filtered before ranking

print(rag.audit[-1])  # {"user": "alice", "query": ..., "returned": [], "denied_chunks": 1}
```

### HTTP

Both servers:

```
GET /query?user=<id>&q=<query>
→ {"results": [{"id", "doc_id", "text", "score"}], "denied_chunks": <int>}
```

Underwriter server only:

```
GET /ask?user=<role>&q=<query>    → adds "answer" + "usage" (or "note" if no API key)
GET /audit                        → {"entries": [last 50 audit records]}
```

**curl**

```bash
curl "http://127.0.0.1:8420/query?user=bob&q=salary+bands"
```

**JavaScript**

```javascript
const res = await fetch("http://127.0.0.1:8420/query?user=bob&q=" + encodeURIComponent("salary bands"));
const { results, denied_chunks } = await res.json();
```

**Python (client)**

```python
import urllib.request, json
with urllib.request.urlopen("http://127.0.0.1:8420/query?user=bob&q=salary+bands") as r:
    data = json.load(r)
```

## Design notes

- **Pre-filter vs post-filter:** post-filtering (rank everything, drop forbidden results)
  leaks through scores, ranks, and "N results hidden" side channels tied to specific
  queries. This library filters the candidate set first; forbidden chunks are never scored.
- **Ranking:** in-memory TF-IDF cosine. Retrieval quality is deliberately simple — the
  contribution here is the permission model. Swap `_score()` for embedding cosine
  (e.g. sentence-transformers) when quality matters; the ACL logic doesn't change.
- **Tests:** `app/test_permission_rag.py` includes the leak test — an exact-content
  query for a forbidden document must return nothing.
