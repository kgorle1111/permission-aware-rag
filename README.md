# Permission-Aware RAG

RAG retrieval that enforces per-user document ACLs **at query time**. The core security
property: chunks the caller cannot read are excluded **before** ranking (pre-filtering),
and BM25 statistics (df, corpus size, average length) are computed over the
caller-visible set only — so denied content cannot influence scores, ordering,
citations, or even relative score shifts. The classic enterprise-RAG leak ("the vector
index ignores document permissions") is impossible by construction, and the eval
harness proves it on every CI run.

Zero dependencies — Python standard library only.

## Architecture

```
app/
├── permission_rag.py     # core: ACL pre-filter → BM25 rank → hash-chained audit log
├── llm.py                # one Claude call (Haiku 4.5, urllib), injection boundary,
│                         #   citation verification, prompt caching on system prompt
├── underwriter_server.py # product server: roles/JWT, corpus, /query /ask /audit /presets
├── ui.html               # workbench UI (vanilla JS, single file)
├── presets.json          # sidebar workflows — shops edit JSON, not HTML
├── demo_server.py        # generic 4-user demo (self-contained page)
├── evals.json            # 20 per-role cases: expected docs AND must-not-return docs
├── run_evals.py          # recall@4 + leak-rate runner; exits 1 on any leak (CI gate)
└── test_*.py             # leak, ACL, LLM (mocked), and HTTP endpoint tests
```

Flow: deterministic guards (ACL pre-filter, empty-ACL refusal, duplicate-ingest
refusal) → one structured LLM call (grounded-only, citations required + post-hoc
verified; skipped gracefully without an API key or on zero results) →
human-review-always-wins (findings language, never decisions) → tamper-evident
audit trail (JSONL, sha256 hash chain, survives restarts).

Docs: `CASE_FILE.md` (use case, caching design, risks) · `INTEGRATION.md`
(identity-in / documents-in / answers-out seams) · `BACKLOG.md` (the 2026-07-20
improvement backlog — all seven waves complete, including every security finding).

## What it does

- Ingest documents with an ACL: `user:<id>`, `group:<name>`, or `"*"` (public)
- Sentence-boundary chunking with a one-sentence overlap; `remove_document()` for re-sync
- Retrieve with a user identity → only chunks that user may read are scored (BM25) and returned
- Every retrieval is hash-chain audited: who, what, returned ids, denied count, elapsed_ms
- Empty ACLs and duplicate doc_ids are rejected at ingest

## How to use

**Pinokio:** click **Start Underwriter Workbench** (or **Start Generic Demo**), then
**Open**. Pick a role (keyboard `1–4`), type a query (`/` to focus), and watch the same
question return different results per role. Deep links (`/?user=senior&q=...`),
clickable citations, copy-as-file-note, and copy-as-curl are built in.

**Manual:**

```bash
cd app
uv run pytest                       # full suite (leak, ACL, LLM-mocked, HTTP)
python3 run_evals.py                # 20-case eval: recall@4 + leak rate
python3 demo_server.py 8420         # generic demo UI
python3 underwriter_server.py 8421  # underwriter workbench
```

Lint/format: `ruff check .` / `ruff format .` (config in `pyproject.toml`; enforced in CI
alongside pytest and the eval leak gate).

## Configuration (env vars)

| Var | Effect |
|---|---|
| `ANTHROPIC_API_KEY` | Enables `/ask` drafted answers (Haiku 4.5, ~$0.002/query). Without it, retrieval-only. |
| `UNDERWRITER_JWT_SECRET` | Switches identity to `Authorization: Bearer <HS256 JWT>` (`sub` + `groups` claims). Demo dropdown otherwise. |
| `SHOW_DENIED=0` | Removes the denied-chunk count from responses (it's a deliberate demo side channel). |

## API

### Python (library)

```python
from permission_rag import PermissionRAG

rag = PermissionRAG(audit_path="audit_log.jsonl")  # audit_path optional
rag.add_document("salaries", "salary bands range from 90k to 250k", {"group:hr"})

hr_user = {"id": "bob", "groups": ["hr"]}
results = rag.retrieve("salary bands", hr_user, k=3)
# [{"id": "salaries#0", "doc_id": "salaries", "text": ..., "score": 1.79}]  # BM25, unbounded

rag.retrieve("salary bands", {"id": "alice", "groups": ["eng"]})  # [] — filtered before ranking

rag.remove_document("salaries")                    # re-sync = remove + add
PermissionRAG.verify_audit_chain("audit_log.jsonl")  # True unless the log was tampered with
```

### HTTP (underwriter server)

`POST` with a JSON body is preferred (keeps questions out of proxy logs); `GET` with
query params still works for deep links.

```
POST /query   {"user": "<role>", "q": "<query>"}
→ {"results": [{"id","doc_id","text","score"}], "denied_chunks": <int>}

POST /ask     {"user": "<role>", "q": "<query>"}     (rate-limited: 10/min/IP)
→ adds "answer", "usage", "llm_ms", "est_cost_usd", "unverified_citations"
  (or "note" when there's no API key / no results)

GET /audit?user=<role>              → {"entries": [...], "llm_summary": {...}}
GET /audit?user=<role>&format=csv   → CSV download (same per-caller scoping)
GET /presets                        → sidebar workflow presets (presets.json)
```

Audit visibility: non-audit roles see only their own entries; the `audit` group sees all.

**curl**

```bash
curl -s -X POST http://127.0.0.1:8421/ask -H 'content-type: application/json' \
  -d '{"user":"senior","q":"can we bind Delgado above 1 million?"}'
```

(The UI's "Copy as curl" button generates this for any query.)

## Design notes

- **Pre-filter vs post-filter:** post-filtering (rank everything, drop forbidden results)
  leaks through scores, ranks, and count side channels. This library filters the candidate
  set first, and computes ranking statistics over the visible set only — verified by the
  S1 regression test (identical scores with and without a hidden doc present).
- **Ranking:** in-memory BM25 (k1=1.5, b=0.75). Retrieval quality is deliberately simple —
  the contribution is the permission model. Swap `_score()` for embedding cosine when
  recall matters; the ACL logic doesn't change.
- **Prompt injection:** retrieved text is framed in `<document>` tags and declared
  data-not-instructions; citations the model produces are verified post-hoc against the
  retrieved set (`unverified_citations`).
- **Evals as the gate:** the BM25 swap and the chunking change both merged only after
  `run_evals.py` showed unchanged recall and zero leaks. CI runs it on every push.
