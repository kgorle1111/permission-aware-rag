"""Postgres + pgvector backend where the ACL is enforced by the DATABASE, not the app.

The in-memory PermissionRAG enforces permissions in Python. This backend pushes the
same guarantee down into Postgres Row-Level Security:

- Every chunk row carries three independent ACL arrays.
- A SELECT policy admits a row only when every ACL level overlaps the caller's principals,
  supplied per-transaction via `set_config('rag.principals', ...)` (parameterized —
  no SQL string building).
- The app role is a non-superuser and not the table owner, so RLS applies to every
  query it runs. With no principals set, the table is EMPTY. A query that forgets
  its filter cannot see a forbidden row — the pre-filter guarantee becomes a
  database property instead of an application promise. Not a full SQL-injection
  defense: SQL on the app connection can still set `rag.principals` itself (T18).
- Ranking is pgvector cosine (`<=>`) over the RLS-filtered rows. Embedding distance
  is per-row (no corpus statistics), so the BM25 side channel (S1) has no analogue
  here by construction.

Ingest runs as a separate database role on its own connection. The app role has SELECT
only on chunks and no membership in the ingest role, so no setting it can change grants
write access or the ingest policy (T13). Without an ingest DSN the instance is read-only.
Audit rows are hash-chained exactly like the JSONL backend.

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
    acl_doc   text[] NOT NULL,
    acl_section text[] NOT NULL,
    acl_para  text[] NOT NULL,
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
-- Existing flat rows have no child restrictions. Migrate before changing policy.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS acl_doc text[];
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS acl_section text[];
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS acl_para text[];
UPDATE chunks SET acl_doc = acl WHERE acl_doc IS NULL;
UPDATE chunks SET acl_section = ARRAY['*'] WHERE acl_section IS NULL;
UPDATE chunks SET acl_para = ARRAY['*'] WHERE acl_para IS NULL;
ALTER TABLE chunks ALTER COLUMN acl_doc SET NOT NULL;
ALTER TABLE chunks ALTER COLUMN acl_section SET NOT NULL;
ALTER TABLE chunks ALTER COLUMN acl_para SET NOT NULL;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS chunks_read ON chunks;
CREATE POLICY chunks_read ON chunks FOR SELECT
    USING (acl_doc && string_to_array(current_setting('rag.principals', true), ',')
       AND acl_section && string_to_array(current_setting('rag.principals', true), ',')
       AND acl_para && string_to_array(current_setting('rag.principals', true), ','));
DROP POLICY IF EXISTS chunks_ingest ON chunks;
"""

# REVOKEs migrate databases created when the app role still wrote chunks (T13).
ROLE_GRANTS = """
GRANT USAGE ON SCHEMA public TO {app}, {ingest};
REVOKE INSERT, UPDATE, DELETE ON chunks FROM {app};
REVOKE UPDATE ON corpus_stats FROM {app};
GRANT SELECT ON chunks, corpus_stats TO {app};
GRANT SELECT, INSERT ON audit TO {app};
GRANT USAGE ON SEQUENCE audit_id_seq TO {app};
GRANT SELECT, INSERT, DELETE ON chunks TO {ingest};
GRANT SELECT, UPDATE ON corpus_stats TO {ingest};
CREATE POLICY chunks_ingest ON chunks FOR ALL TO {ingest} USING (true) WITH CHECK (true)
"""


def setup_schema(
    admin_dsn: str,
    app_role: str = "rag_app",
    app_password: str = "rag_app",
    ingest_role: str = "rag_ingest",
    ingest_password: str = "rag_ingest",
) -> None:
    """Run once as a privileged role: extension, tables, RLS policies, both roles.

    Neither role gets BYPASSRLS or owns the tables, so every query they run is subject
    to the policies. Neither is a member of the other, so SET ROLE can't cross over.
    """
    with psycopg.connect(admin_dsn) as conn:
        conn.execute(SCHEMA)
        for role, password in ((app_role, app_password), (ingest_role, ingest_password)):
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                # role/password come from trusted config; composed via sql.Identifier/Literal
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        for stmt in ROLE_GRANTS.strip().split(";"):
            conn.execute(
                sql.SQL(stmt).format(app=sql.Identifier(app_role), ingest=sql.Identifier(ingest_role))
            )


class PgVectorRAG:
    """Drop-in for PermissionRAG backed by Postgres RLS + pgvector."""

    audit_path = None  # interface compat with the JSONL backend

    def __init__(self, dsn: str, ingest_dsn: str | None = None):
        """dsn connects as the app role (retrieval); ingest_dsn as the ingest role."""
        # autocommit: single reads commit immediately (no idle-in-transaction locks);
        # multi-statement work still uses explicit conn.transaction() blocks, which
        # is also what scopes each set_config(..., is_local=true) GUC.
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.ingest_conn = psycopg.connect(ingest_dsn, autocommit=True) if ingest_dsn else None

    def close(self) -> None:
        self.conn.close()
        if self.ingest_conn:
            self.ingest_conn.close()

    # ── ingest (separate DB role, separate connection) ───────────────────────
    def _writer(self) -> psycopg.Connection:
        if self.ingest_conn is None:
            raise PermissionError("read-only PgVectorRAG: pass ingest_dsn (the ingest role) to ingest")
        return self.ingest_conn

    def add_document(
        self, doc_id: str, text: str, acl, chunk_words: int = 80, *, sections: list[dict] | None = None
    ) -> None:
        chunks = PermissionRAG.document_chunks(text, acl, chunk_words, sections=sections)
        w = self._writer()
        with w.transaction():
            dup = w.execute("SELECT 1 FROM chunks WHERE doc_id = %s LIMIT 1", (doc_id,)).fetchone()
            if dup:
                raise ValueError(f"doc_id {doc_id!r} already ingested — use remove_document() then re-add")
            for i, chunk in enumerate(chunks):
                chunk_text = chunk["text"]
                w.execute(
                    "INSERT INTO chunks (id, doc_id, text, acl, acl_doc, acl_section, acl_para, embedding) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                    (
                        f"{doc_id}#{i}",
                        doc_id,
                        chunk_text,
                        sorted(chunk["acl_doc"]),
                        sorted(chunk["acl_doc"]),
                        sorted(chunk["acl_section"]),
                        sorted(chunk["acl_para"]),
                        to_pgvector(embed(chunk_text)),
                    ),
                )
            w.execute("UPDATE corpus_stats SET chunk_count = (SELECT count(*) FROM chunks)")

    def remove_document(self, doc_id: str) -> int:
        w = self._writer()
        with w.transaction():
            cur = w.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
            w.execute("UPDATE corpus_stats SET chunk_count = (SELECT count(*) FROM chunks)")
            return cur.rowcount

    # ── retrieval (rag.principals only — RLS does the filtering) ─────────────
    @staticmethod
    def principals(user: dict) -> list[str]:
        names = [user["id"], *user.get("groups", ())]
        # ',' is the GUC delimiter; an embedded one would forge an extra principal
        if any(not n or "," in n for n in names):
            raise ValueError("user id and group names must be non-empty and contain no comma")
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
                "query": "[redacted]",
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
        for line, recorded_hash in self.conn.execute("SELECT line, line_sha256 FROM audit ORDER BY id ASC"):
            try:
                entry = json.loads(line)
                if not isinstance(entry, dict) or entry.get("prev_sha256") != prev:
                    return False
            except ValueError:
                return False
            prev = hashlib.sha256(line.encode()).hexdigest()
            if prev != recorded_hash:
                return False
        return True
