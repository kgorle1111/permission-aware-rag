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
| T08 | Cross-user audit query read | Caller sees own queries; audit group sees others' ids/counts with query text redacted and chain hashes omitted | `app/test_security.py` "def test_audit_query_redaction" | controlled |
| T09 | Document text or id closes the prompt's data boundary | Escape text and ids before framing; system prompt says data-not-instructions. Semantic prompt injection remains possible | `app/test_security.py` "def test_document_breakout" | controlled (structural breakout only) |
| T10 | Model cites a doc it wasn't given | Post-hoc check surfaces unknown bracketed ids, including slashes, colons and spaces; this does not verify claim support | `app/test_security.py` "def test_citations_with_real_document_ids" | controlled |
| T11 | Hidden-document count reveals that forbidden docs exist | `SHOW_DENIED=0` removes it outside the demo | `app/test_http.py` "SHOW_DENIED off" | accepted (on in demo by design) |
| T12 | Edit or truncate the newest audit entry | JSONL chain checked against a separate local head checkpoint; pgvector checks stored line hashes. Rewriting both log and checkpoint needs an independently retained head; pgvector tail deletion remains unanchored | `app/test_security.py` "def test_audit_tail_tamper"; `app/test_security.py` "def test_audit_checkpoint_is_required"; `app/test_pgvector.py` "def test_newest_audit_entry_tamper" | controlled for edits and JSONL-only truncation; broader storage attacker accepted |
| T13 | SQL execution on the app connection sets `rag.mode='ingest'` / forges `rag.principals` | none yet; GUCs are settable by the app role | none — ROADMAP: scoped, not scheduled | open |
| T14 | Signed JWT with a comma in a group → unhandled 500 on pgvector | Fails closed (no rows), but not a clean 401 | none — ROADMAP B06 | open |
| T15 | Aggregation: combining permitted docs to infer a forbidden fact | Out of scope for retrieval ACLs; human review of drafts | none | open |
| T16 | Formula execution when opening an audit CSV | Prefix dangerous cells with an apostrophe at export; source audit stays unchanged | `app/test_security.py` "def test_audit_csv_formula_injection" | controlled |
| T17 | Non-object JSON or invalid field types cause a request crash | Explicit body and query type checks; retrieval failures return 503 without results | `app/test_security.py` "def test_non_object_body_is_400"; `app/test_security.py` "def test_invalid_field_types_are_400"; `app/test_security.py` "def test_retrieval_error_is_503" | controlled |
