# Text-to-SQL under Postgres RLS, 2026-10-10

Command: `DATABASE_URL=... python3 app/eval_text_to_sql.py` (disposable PostgreSQL 16 + pgvector).
The model is a deterministic lookup (question -> SQL): **no LLM was called**, so this measures the
database path and the permission guarantee, not any model's SQL quality. Gold answers come from an
independent Python oracle over `PermissionRAG.can_read`, not from the database under test.

| check | result |
|---|---|
| Honest questions, exact match against the oracle | **60/60** (12 questions x 5 users) |
| Hidden-document ids/titles in any result (honest + hostile) | **0** |
| Hostile statements per user (pre-check OFF, database alone) | 40 x 5 = 200 runs |
| ...that returned an error | 165 |
| ...that returned rows (all visible-only, none containing hidden ids/titles) | 35 |
| Audit rows written by the database | 260 (one per run), hash chain verifies: True |

Users: the four demo roles plus a guest with no groups; corpus: the 7 demo documents plus the 2 trap
documents (`group:underwriting-admin`, `group:banking*`) that catch prefix/wildcard matching.
Limits: 12 hand-written questions, a fixed 9-row table, and no model in the loop. Accuracy here
says nothing about how often a real model writes correct SQL, and a correct-looking count over
visible rows is still only as complete as the caller's permissions.
