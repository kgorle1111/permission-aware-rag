# Threat model

Every **controlled** or **accepted** row cites the check that pins it. `app/test_ledgers.py`
fails if a cited file or anchor disappears. **open** rows are known gaps, listed
so nobody mistakes silence for safety.

| Id | Threat | Control | Test | Status |
|---|---|---|---|---|
| T01 | Forbidden doc returned for an exact-content query | Pre-filter before ranking (in-memory); RLS policy (pgvector) | `app/test_permission_rag.py` "THE leak test"; `app/eval_scale.py` "must-not trial" | controlled |
| T02 | Hidden doc shifts visible scores via corpus statistics | Visible-set BM25 stats; pgvector scores are per-row | `app/test_permission_rag.py` "S1: IDF side channel"; `app/run_evals.py` "def isolation_gate" | controlled |
| T03 | Bare-string ACL iterated into characters (`"*"` inside → public) | `normalize_acl` rejects str/bytes | `app/test_permission_rag.py` "a bare str must not be iterated" | controlled |
| T04 | Malformed ACL entry (empty, comma, trailing newline) | `fullmatch` on a strict entry pattern | `app/test_permission_rag.py` "group:hr\n" | controlled |
| T05 | Comma in a user id/group forges an extra principal in the RLS GUC | `principals()` rejects commas and empties | `app/test_pgvector.py` "def test_acl_and_principal_boundary" | controlled |
| T06 | Result cache shared across principals | No cache exists; the isolation gate catches a principal-less cache | `app/mutants.py` "class SharedCache" | controlled |
| T07 | Prefix or wildcard group matching (`group:hr` reads `group:hr-admin`) | Exact set membership in `can_read`; trap ACLs in both evals | `app/mutants.py` "class GroupPrefix"; `app/run_evals.py` "group:underwriting-admin" | controlled |
| T08 | Cross-user audit read | `/audit` scoped to caller; full view gated to the audit group | `app/test_http.py` "/audit scoping" | controlled |
| T09 | Prompt injection via retrieved doc text | Docs framed as `<document>` data; system prompt says data-not-instructions | `app/test_llm.py` "injection boundary" | controlled |
| T10 | Model cites a doc it wasn't given | Post-hoc citation check surfaces `unverified_citations` | `app/test_llm.py` "citation verification" | controlled |
| T11 | Hidden-document count reveals that forbidden docs exist | `SHOW_DENIED=0` removes it outside the demo | `app/test_http.py` "SHOW_DENIED off" | accepted (on in demo by design) |
| T12 | Audit tail truncation: deleting the last lines leaves a valid chain | Chain detects edits and removal of any non-tail line | `app/test_permission_rag.py` "tamper first entry" | accepted (anchor the head hash externally when a compliance buyer asks) |
| T13 | SQL execution on the app connection sets `rag.mode='ingest'` / forges `rag.principals` | none yet; GUCs are settable by the app role | none — PLAN Stage 4.1 | open |
| T14 | Signed JWT with a comma in a group → unhandled 500 on pgvector | Fails closed (no rows), but not a clean 401 | none — PLAN 0.3b | open |
| T15 | Aggregation: combining permitted docs to infer a forbidden fact | Out of scope for retrieval ACLs; human review of drafts | none | open |
