"""Postgres + pgvector backend where the ACL is enforced by the DATABASE, not the app.

The in-memory PermissionRAG enforces permissions in Python. This backend pushes the
same guarantee down into Postgres Row-Level Security:

- Every chunk row carries an `acl text[]`.
- A SELECT policy admits a row only when its acl overlaps the caller's principals,
  supplied per-transaction via `set_config('rag.principals', ...)` (parameterized —
  no SQL string building).
- The app role is a non-superuser and not the table owner, so RLS applies to every
  query it runs. With no principals set, the table is EMPTY. Even a SQL injection
  through this connection cannot see a forbidden row — the pre-filter guarantee
  becomes a database property instead of an application promise.
- Ranking is pgvector cosine (`<=>`) over the RLS-filtered rows. Embedding distance
  is per-row (no corpus statistics), so the BM25 side channel (S1) has no analogue
  here by construction.

Ingest runs in a separate mode (`rag.mode = 'ingest'`) granted by its own policy;
retrieval never sets it. Audit rows are hash-chained exactly like the JSONL backend.

Same public surface as PermissionRAG: add_document / remove_document / retrieve /
audit / verify_audit_chain. Requires `psycopg` (the project's only optional dep).
"""

import hashlib
import json
import time

import psycopg
from embedding import DIMS, embed, to_pgvector
from permission_rag import PermissionRAG
from psycopg import sql

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS chunks (
    id        text PRIMARY KEY,
    doc_id    text NOT NULL,
    text      text NOT NULL,
    acl       text[] NOT NULL CHECK (cardinality(acl) > 0),
    embedding vector({DIMS}) NOT NULL
);
CREATE TABLE IF NOT EXISTS corpus_stats (
    id integer PRIMARY KEY CHECK (id = 1),
    chunk_count integer NOT NULL
);
INSERT INTO corpus_stats (id, chunk_count) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
CREATE TABLE IF NOT EXISTS audit (
    id bigserial PRIMARY KEY,
    -- raw JSON line, hashed as-is: jsonb would normalize key order/floats and
    -- break chain verification
    line text NOT NULL,
    line_sha256 text NOT NULL
);
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS chunks_read ON chunks;
CREATE POLICY chunks_read ON chunks FOR SELECT
    USING (acl && string_to_array(current_setting('rag.principals', true), ','));
DROP POLICY IF EXISTS chunks_ingest ON chunks;
CREATE POLICY chunks_ingest ON chunks FOR ALL
    USING (current_setting('rag.mode', true) = 'ingest')
    WITH CHECK (current_setting('rag.mode', true) = 'ingest');
"""

APP_ROLE_GRANTS = """
GRANT USAGE ON SCHEMA public TO {role};
GRANT SELECT, INSERT, DELETE ON chunks TO {role};
GRANT SELECT ON corpus_stats TO {role};
GRANT UPDATE ON corpus_stats TO {role};
GRANT SELECT, INSERT ON audit TO {role};
GRANT USAGE ON SEQUENCE audit_id_seq TO {role};
"""


def setup_schema(admin_dsn: str, app_role: str = "rag_app", app_password: str = "rag_app") -> None:
    """Run once as a privileged role: extension, tables, RLS policies, app role.

    The app role deliberately gets no BYPASSRLS and does not own the tables,
    so every query it runs is subject to the policies above.
    """
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(SCHEMA)
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,)).fetchone()
        if not exists:
            # role/password come from trusted config; composed via sql.Identifier/Literal
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(app_role), sql.Literal(app_password)
                )
            )
        for stmt in APP_ROLE_GRANTS.strip().split(";"):
            if stmt.strip():
                conn.execute(sql.SQL(stmt).format(role=sql.Identifier(app_role)))


class PgVectorRAG:
    """Drop-in for PermissionRAG backed by Postgres RLS + pgvector."""

    audit_path = None  # interface compat with the JSONL backend

    def __init__(self, dsn: str):
        # autocommit: single reads commit immediately (no idle-in-transaction locks);
        # multi-statement work still uses explicit conn.transaction() blocks, which
        # is also what scopes each set_config(..., is_local=true) GUC.
        self.conn = psycopg.connect(dsn, autocommit=True)

    def close(self) -> None:
        self.conn.close()

    # ── ingest (rag.mode = 'ingest') ─────────────────────────────────────────
    def add_document(self, doc_id: str, text: str, acl, chunk_words: int = 80) -> None:
        if not acl:
            raise ValueError("acl must not be empty — refusing to ingest unreadable/ambiguous document")
        with self.conn.transaction():
            self.conn.execute("SELECT set_config('rag.mode', 'ingest', true)")
            dup = self.conn.execute("SELECT 1 FROM chunks WHERE doc_id = %s LIMIT 1", (doc_id,)).fetchone()
            if dup:
                raise ValueError(f"doc_id {doc_id!r} already ingested — use remove_document() then re-add")
            for i, chunk_text in enumerate(PermissionRAG._chunk_texts(text, chunk_words)):
                self.conn.execute(
                    "INSERT INTO chunks (id, doc_id, text, acl, embedding) VALUES (%s,%s,%s,%s,%s::vector)",
                    (f"{doc_id}#{i}", doc_id, chunk_text, list(acl), to_pgvector(embed(chunk_text))),
                )
            self.conn.execute("UPDATE corpus_stats SET chunk_count = (SELECT count(*) FROM chunks)")

    def remove_document(self, doc_id: str) -> int:
        with self.conn.transaction():
            self.conn.execute("SELECT set_config('rag.mode', 'ingest', true)")
            cur = self.conn.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
            self.conn.execute("UPDATE corpus_stats SET chunk_count = (SELECT count(*) FROM chunks)")
            return cur.rowcount

    # ── retrieval (rag.principals only — RLS does the filtering) ─────────────
    @staticmethod
    def principals(user: dict) -> list[str]:
        return ["*", f"user:{user['id']}"] + [f"group:{g}" for g in user.get("groups", ())]

    def retrieve(self, query: str, user: dict, k: int = 3) -> list[dict]:
        t0 = time.perf_counter()
        qvec = to_pgvector(embed(query))
        with self.conn.transaction():
            self.conn.execute(
                "SELECT set_config('rag.principals', %s, true)", (",".join(self.principals(user)),)
            )
            rows = self.conn.execute(
                "SELECT id, doc_id, text, 1 - (embedding <=> %s::vector) AS score "
                "FROM chunks ORDER BY embedding <=> %s::vector LIMIT %s",
                (qvec, qvec, k),
            ).fetchall()
            visible = self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
            total = self.conn.execute("SELECT chunk_count FROM corpus_stats").fetchone()[0]
        results = [
            {"id": r[0], "doc_id": r[1], "text": r[2], "score": round(float(r[3]), 4)}
            for r in rows
            if r[3] > 0
        ]
        self._append_audit(
            {
                "ts": time.time(),
                "user": user["id"],
                "query": query,
                "returned": [r["id"] for r in results],
                "denied_chunks": total - visible,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            }
        )
        return results

    # ── audit (hash-chained, same scheme as the JSONL backend) ───────────────
    def _append_audit(self, entry: dict) -> None:
        with self.conn.transaction():
            last = self.conn.execute("SELECT line_sha256 FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            entry["prev_sha256"] = last[0] if last else ""
            line = json.dumps(entry)
            self.conn.execute(
                "INSERT INTO audit (line, line_sha256) VALUES (%s, %s)",
                (line, hashlib.sha256(line.encode()).hexdigest()),
            )

    @property
    def audit(self) -> list[dict]:
        rows = self.conn.execute("SELECT line FROM audit ORDER BY id DESC LIMIT 1000").fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def verify_audit_chain(self) -> bool:
        prev = ""
        for (line,) in self.conn.execute("SELECT line FROM audit ORDER BY id ASC"):
            if json.loads(line).get("prev_sha256") != prev:
                return False
            prev = hashlib.sha256(line.encode()).hexdigest()
        return True
