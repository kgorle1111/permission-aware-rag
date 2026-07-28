# Permission-Aware RAG

A retrieval system that enforces **per-user document permissions at query time** —
forbidden chunks are excluded **before ranking**, so the intern can never query
their way into the CEO's compensation data. With an audit trail of every retrieval,
permission sync from the source system, and a leakage test suite that proves it.

> Glean proved permission-aware retrieval is the moat; frameworks give you a filter
> parameter and wish you luck. This is the open layer in between: chunk-level ACLs,
> identity-scoped retrieval, permission sync, and an audit trail — with leakage
> tests that prove it.

**The demo:** one query — *"what are the salary bands?"* — three users:
HR gets the answer with a citation; engineering gets nothing; guest gets nothing.
The audit log shows `denied: 3` for the guest. Run it: `python scripts/demo.py`.

---

## What the spec demands (the bar)

From the *Permission-Aware RAG — Complete Build Guide*:

1. **Pre-filtering, not post-filtering** — the ACL predicate is evaluated *inside*
   the vector engine (Qdrant payload filter during traversal). There is no code
   path where an unreadable chunk meets the scorer, so counts, ranks, and scores
   cannot leak. This is the core security argument.
2. **Chunk-level ACL inheritance** — every chunk inherits its document's ACL at
   ingestion; section overrides apply **strictest-wins**; empty/malformed ACLs
   collapse to owner-only (deny by default), never to public.
3. **Identity propagation** — RS256 JWTs verified per request; the API holds only
   the IdP's public key. Groups come from signed claims, never from anything the
   client asserts. No token, bad token, expired token, wrong audience, forged
   signature → 401, no retrieval.
4. **Permission sync & measured staleness** — a sync loop reconciles source-system
   ACL changes into Postgres (source of truth) then the Qdrant payload, then kills
   cached answers. Revocation propagation is measured in the test suite, not
   hand-waved. Google Drive connector included (`changes.list` delta + webhook).
5. **Audit logging** — every retrieval writes who asked, what, which chunks
   returned, and **how many were hidden by ACL** — the insider-risk signal no
   vanilla RAG provides. Cache hits are audited too.
6. **Side channels closed** — "no results" is byte-identical whether the topic is
   forbidden or nonexistent; the answer cache key includes the caller's permission
   scope; generation is built only from permitted chunks, so even a jailbroken
   model has nothing to reveal.
7. **Fail closed** — if the permission layer errors, the answer is *nothing*,
   never unfiltered results. (Cost Forensics fails **open** — observability must
   never break the product. Same architecture question, opposite correct answer,
   because the failure costs are opposite.)
8. **Local embeddings as a security decision** — confidential text never transits
   a third-party API to become a vector.

## Tech stack

| Layer | Pick | Why |
|---|---|---|
| API | **FastAPI (async)** | Pydantic validation at the trust boundary; `Depends(verify_jwt)` runs before any handler. |
| Vector store | **Qdrant** (embedded local mode default, `QDRANT_URL` for server) | ACL payload filter evaluated during HNSW traversal — security *and* recall survive selective filters. Alternatives: pgvector (one DB for everything), Weaviate, Pinecone (SaaS-only, weaker fit for a security product). |
| Embeddings | **Local** — hashing-trick default, `EMBED_BACKEND=st` for sentence-transformers (bge-small) | Text never leaves your infra. The permission model's guarantees don't depend on embedding quality; retrieval quality does — one env var upgrades it. |
| ACL source of truth + audit | **SQLAlchemy → SQLite (dev) / Postgres (prod)** | A half-applied revocation or dropped audit row is a security bug — ACID or nothing. `config/postgres_rls.sql` adds Row-Level Security as defense in depth. |
| Identity | **PyJWT, RS256 only** | The API can verify, never forge. Short expiry named as the revocation tradeoff — same shape as the ACL staleness window. |
| Permission sync | **Reconciliation poll + webhook path**; synthetic `permissions.jsonl` source; **Drive connector** (`app/connectors/gdrive.py`) | The poll interval is the stated worst-case staleness bound. |
| AuthZ ceiling | **Flat ACLs now; OpenFGA named as the roadmap** | Flat `user:/group:/*` sets hold until nested folders/groups appear; then materialize OpenFGA ListObjects into the same chunk payload field — hot path never calls OpenFGA. |
| Generation | **Extractive (keyless) / Anthropic haiku with grounded system prompt** | Prompt assembled only from permitted chunks. |

## Architecture

```
   Alice(eng)      Bob(hr)       Guest
      │               │             │   Bearer JWT (RS256, signed by IdP)
      ▼               ▼             ▼
 ┌─────────────────────────────────────────┐
 │ IDENTITY  verify signature, aud, exp    │  fail closed: 401, no retrieval
 └───────────────────┬─────────────────────┘
                     ▼ (user, groups) as verified fact
 ┌─────────────────────────────────────────┐     ┌──────────────────────────┐
 │ 1· ACL PRE-FILTER + 2· RANKER           │◄────│ VECTOR INDEX (Qdrant)    │
 │    one filtered HNSW query — forbidden  │     │ every chunk carries acl:[]│
 │    chunks are never scored              │     └──────────▲───────────────┘
 └───────────────────┬─────────────────────┘                │ ingest: chunk+embed
                     ▼                                      │ +inherit doc ACL
 ┌───────────────────────────┐   ┌───────────────────┐   ┌──┴────────────────┐
 │ 3· AUDIT LOG (Postgres)   │   │ PERMISSION SYNC   │◄──│ SOURCE SYSTEMS    │
 │ who/what/returned/denied  │   │ poll + webhook    │   │ Drive / jsonl     │
 └───────────────────────────┘   └───────────────────┘   └───────────────────┘
                     ▼
 ┌─────────────────────────────────────────┐
 │ GENERATION — prompt built ONLY from     │
 │ permitted chunks (jailbreak-proof by    │
 │ construction), citations user can open  │
 └─────────────────────────────────────────┘
```

## Workflow (how a query flows)

1. `POST /query` with a Bearer JWT → signature/audience/expiry verified against
   the IdP public key; principals = `[user:<sub>, group:<g>..., *]`.
2. Permission-scoped cache check (key = principal scope + query). Hits are audited.
3. Query embedded **locally** → one Qdrant search with `acl MatchAny(principals)`
   evaluated during traversal + a score floor so no-match is decisively no-match.
4. An internal unfiltered count computes `denied_count` (the number never leaves
   the server — only the count reaches the audit log).
5. Any error anywhere → empty response, `fail_closed=true` audit row.
6. Answer generated from permitted chunks only; uniform "No results found." body
   whether content is forbidden or nonexistent.
7. Meanwhile the sync loop reconciles source ACL changes: Postgres → Qdrant
   payload → cache invalidation. Staleness = poll interval, measured in tests.

## Repo layout

```
app/
  main.py        /query · /audit (group:security) · /sync · demo UI
  identity.py    RS256 JWT verification → Principal (fail closed)
  retrieval.py   pre-filter pipeline, scoped cache, fail-closed, audit
  vectorstore.py Qdrant filtered search + payload ACL updates
  embeddings.py  local embedder (hash default / sentence-transformers)
  ingest.py      chunking + ACL inheritance (strictest-wins, deny-by-default)
  sync.py        reconciliation loop, measured staleness
  audit.py       trail + denied-query heatmap
  generation.py  grounded answers from permitted chunks only
  connectors/gdrive.py   Drive changes.list + permission normalization
corpus/          demo docs with planted CANARY strings + permissions.jsonl
scripts/         gen_keys · mint_token · ingest · sync_run · demo
static/index.html   demo UI: user picker · query · audit panel
tests/           26 tests — leakage probes, side channels, revocation,
                 injection, fail-closed, forged JWTs
config/postgres_rls.sql   Row-Level Security for production Postgres
```

## Quickstart

Full guide in **[SETUP.md](SETUP.md)**. TL;DR (no API keys needed):

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
python scripts/gen_keys.py       # local IdP keypair
python scripts/ingest.py         # 12 chunks, ACLs inherited
DEMO_MODE=1 uvicorn app.main:app --port 8090 &
python scripts/demo.py           # same query, 3 users, 3 answers + audit
open http://localhost:8090       # interactive demo UI
pytest tests/ -q                 # 26 passing — the leakage suite IS the product
```

## The three-number metric (top of every pitch)

**Zero leaked chunks across 40+ adversarial probes · sub-10 ms P95 retrieval
(local demo corpus) · revocations propagate in one sync pass (< 5 s bound,
measured).**

## The interview pairing (memorize)

> "Cost Forensics fails **open** — observability must never break the product.
> This fails **closed** — if the permission layer is unsure, the answer is
> nothing. Same architecture question, opposite correct answer, because the
> failure costs are opposite."

## What this deliberately does NOT solve (say it unprompted)

1. **Authorized-user exfiltration** — someone with legitimate access leaking what
   they read; the audit log detects, not prevents.
2. **Inference from permitted data** — an LLM reasoning its way to a secret from
   public fragments.
3. **ReBAC nuance** — flat ACLs drop nested folders/groups; the roadmap is
   materializing OpenFGA ListObjects into the same chunk payload field.
4. **Timing side channel** — response bodies are uniform; response *times* are not
   padded.
