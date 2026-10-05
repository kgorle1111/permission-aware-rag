# Decisions

Each row records what we chose, why, and what we rejected. **Evidence** cites a file and
an exact anchor string inside it. `app/test_ledgers.py` fails if a cited file or anchor
disappears, so a decision can't silently outlive its proof.

| Id | Status | Decision | Why | Rejected | Evidence |
|---|---|---|---|---|---|
| D01 | accepted | BM25 (stdlib) ranks the default backend | Term saturation + length normalization; explainable; zero deps. Eval-gated: recall@4 14/14, 0 leaks before and after the swap | TF-IDF (one repeated word dominates); embeddings by default (a dependency with no recall eval to justify it) | `app/permission_rag.py` "K1, B = 1.5, 0.75" |
| D02 | accepted | pgvector backend enforces ACLs with Postgres Row-Level Security, not an app `WHERE` | A forgotten filter in app code returns nothing instead of everything; the DB that holds the rows refuses them | App-side filter in SQL (one missed clause = leak) | `app/test_pgvector.py` "def test_rls_is_the_enforcer_not_the_app" |
| D03 | accepted | Feature-hash embedder (stdlib) for pgvector | Exercises the full RLS + vector path with no model or network | Voyage / sentence-transformers now (cost + dep before a semantic-recall eval exists; add when E4 fails) | `app/embedding.py` "hashing trick" |
| D04 | accepted | BM25 statistics computed over the caller-visible set only | Global IDF lets a hidden doc shift visible scores (S1 side channel) | Global index stats (faster, leaks) | `app/test_permission_rag.py` "S1: IDF side channel" |
| D05 | accepted | ACLs validated once, in `normalize_acl`, which every backend calls | A bare-string ACL iterated into characters; fixing the shared boundary closes the class for every backend | Per-backend checks (one gets forgotten) | `app/test_permission_rag.py` "a bare str must not be iterated" |
| D06 | accepted | Leak gate = hand labels + label-free isolation oracle + scaled rate eval | Hand labels caught 1/6 mutants; the isolation oracle catches 6/6 including score channels; the scaled eval bounds the rate (0/1350, UB 0.28%) | More hand-labeled cases only (doesn't see score channels) | `app/run_evals.py` "def isolation_gate"; `evals/results/2026-10-03-v2/table.md` "Does this eval fail when it should?" |
