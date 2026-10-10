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
  database property instead of an application promise. Principals arrive only as an
  HMAC-signed, 60-second token verified inside Postgres (T18), so SQL on the app connection
  can't widen what it reads. The app role can't SELECT chunks or INSERT audit rows at all:
  every read goes through rag_search, which writes the audit row server-side (T19).
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
import hmac
import json
import secrets
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
-- T18: principals reach RLS only as an HMAC-signed, fresh token, verified with a key the
-- app and ingest roles cannot read. A raw rag.principals setting is ignored.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS rag_secret (id integer PRIMARY KEY CHECK (id = 1), key bytea NOT NULL);
REVOKE ALL ON rag_secret FROM PUBLIC;
CREATE OR REPLACE FUNCTION rag_principals() RETURNS text[]
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
DECLARE
    parts text[] := string_to_array(coalesce(current_setting('rag.token', true), ''), '|');
    k bytea;
BEGIN
    IF coalesce(array_length(parts, 1), 0) <> 3 OR parts[2] !~ '^[0-9]{{1,12}}$'
       OR abs(extract(epoch FROM now()) - parts[2]::bigint) > 60 THEN
        RETURN ARRAY[]::text[];
    END IF;
    SELECT key INTO k FROM rag_secret WHERE id = 1;
    IF k IS NULL OR encode(hmac(convert_to(parts[1] || '|' || parts[2], 'UTF8'), k, 'sha256'), 'hex')
                    IS DISTINCT FROM parts[3] THEN
        RETURN ARRAY[]::text[];
    END IF;
    RETURN string_to_array(parts[1], ',');
END
$fn$;
REVOKE ALL ON FUNCTION rag_principals() FROM PUBLIC;
DROP POLICY IF EXISTS chunks_read ON chunks;
-- (SELECT ...) makes Postgres verify the token once per query, not once per row
CREATE POLICY chunks_read ON chunks FOR SELECT
    USING (acl_doc && (SELECT rag_principals())
       AND acl_section && (SELECT rag_principals())
       AND acl_para && (SELECT rag_principals()));
DROP POLICY IF EXISTS chunks_ingest ON chunks;
-- T19: the app role can't read chunks or write audit rows directly. rag_search (owned by a
-- non-superuser, so RLS still applies inside it) verifies the token, searches, and writes the
-- audit row itself, recording exactly the ids it returns.
CREATE OR REPLACE FUNCTION rag_search(token text, qvec vector, k integer)
RETURNS TABLE (id text, doc_id text, text text, score double precision)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $fn$
#variable_conflict use_column
DECLARE
    t0 timestamptz := clock_timestamp();
    uid text;
    ids text[];
    visible bigint;
    total integer;
    prev text;
    entry text;
BEGIN
    PERFORM set_config('rag.token', coalesce(token, ''), true);
    SELECT substr(x, 6) INTO uid FROM unnest(rag_principals()) AS x WHERE x LIKE 'user:%' LIMIT 1;
    -- top-k by distance, then drop non-positive scores (same semantics as the old client path)
    SELECT coalesce(array_agg(t.cid ORDER BY t.d), ARRAY[]::text[]) INTO ids
      FROM (SELECT c.id AS cid, c.embedding <=> qvec AS d FROM chunks c
            ORDER BY c.embedding <=> qvec LIMIT greatest(least(k, 50), 1)) t
     WHERE 1 - t.d > 0;
    SELECT count(*) INTO visible FROM chunks;
    SELECT s.chunk_count INTO total FROM corpus_stats s;
    PERFORM pg_advisory_xact_lock(hashtext('rag_audit'));  -- serialise the chain
    SELECT a.line_sha256 INTO prev FROM audit a ORDER BY a.id DESC LIMIT 1;
    entry := json_build_object(
        'ts', extract(epoch FROM clock_timestamp()), 'user', uid, 'query', '[redacted]',
        'returned', to_json(ids), 'denied_chunks', total - visible,
        'elapsed_ms', round((extract(epoch FROM clock_timestamp() - t0) * 1000)::numeric, 2),
        'prev_sha256', coalesce(prev, ''))::text;
    INSERT INTO audit (line, line_sha256) VALUES (entry, encode(sha256(convert_to(entry, 'UTF8')), 'hex'));
    RETURN QUERY
      SELECT c.id, c.doc_id, c.text, (1 - (c.embedding <=> qvec))::double precision
        FROM chunks c WHERE c.id = ANY (ids) ORDER BY c.embedding <=> qvec;
END
$fn$;
REVOKE ALL ON FUNCTION rag_search(text, vector, integer) FROM PUBLIC;
"""

# REVOKEs migrate databases created when the app role still wrote chunks (T13).
ROLE_GRANTS = """
GRANT USAGE ON SCHEMA public TO {app}, {ingest}, {definer};
REVOKE INSERT, UPDATE, DELETE ON chunks FROM {app};
REVOKE UPDATE ON corpus_stats FROM {app};
REVOKE SELECT ON chunks, corpus_stats FROM {app};
REVOKE INSERT ON audit FROM {app};
REVOKE USAGE ON SEQUENCE audit_id_seq FROM {app};
GRANT SELECT ON audit TO {app};
GRANT SELECT ON chunks, corpus_stats TO {definer};
GRANT SELECT, INSERT ON audit TO {definer};
GRANT USAGE ON SEQUENCE audit_id_seq TO {definer};
GRANT EXECUTE ON FUNCTION rag_principals() TO {ingest}, {definer};
GRANT CREATE ON SCHEMA public TO {definer};
ALTER FUNCTION rag_search(text, vector, integer) OWNER TO {definer};
GRANT EXECUTE ON FUNCTION rag_search(text, vector, integer) TO {app};
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
    principal_key: bytes | None = None,
    definer_role: str = "rag_definer",
) -> bytes:
    """Run once as a privileged role: extension, tables, RLS policies, both roles.

    Neither role gets BYPASSRLS or owns the tables, so every query they run is subject
    to the policies. Neither is a member of the other, so SET ROLE can't cross over.

    Returns the principal-signing key (T18). Pass it to PgVectorRAG. With no key given, an
    existing key is kept (re-running setup never rotates silently); otherwise a random one is made.
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
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (definer_role,)).fetchone():
            # no LOGIN: reachable only as the owner of rag_search; no BYPASSRLS, so RLS applies
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(definer_role)))
        for stmt in ROLE_GRANTS.strip().split(";"):
            conn.execute(
                sql.SQL(stmt).format(
                    app=sql.Identifier(app_role),
                    ingest=sql.Identifier(ingest_role),
                    definer=sql.Identifier(definer_role),
                )
            )
        if principal_key is None:
            row = conn.execute("SELECT key FROM rag_secret WHERE id = 1").fetchone()
            principal_key = bytes(row[0]) if row else secrets.token_bytes(32)
        if len(principal_key) < 32:
            raise ValueError("principal_key must be at least 32 random bytes")
        conn.execute(
            "INSERT INTO rag_secret (id, key) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET key = EXCLUDED.key",
            (principal_key,),
        )
        return principal_key


def sign_principals(key: bytes, principals: list[str], now: float | None = None) -> str:
    """'p1,p2|unix_ts|hmac_sha256_hex': the only principal channel RLS accepts (60 s window)."""
    body = f"{','.join(principals)}|{int(time.time() if now is None else now)}"
    return f"{body}|{hmac.new(key, body.encode(), hashlib.sha256).hexdigest()}"


class PgVectorRAG:
    """Drop-in for PermissionRAG backed by Postgres RLS + pgvector."""

    audit_path = None  # interface compat with the JSONL backend

    def __init__(self, dsn: str, ingest_dsn: str | None = None, *, principal_key: bytes):
        """dsn connects as the app role (retrieval); ingest_dsn as the ingest role.
        principal_key signs principals for RLS (T18); it must match setup_schema's key."""
        self._principal_key = principal_key
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
        # ',' separates principals and '|' separates token fields; either would forge structure
        if any(not n or "," in n or "|" in n for n in names):
            raise ValueError("user id and group names must be non-empty and contain no ',' or '|'")
        return ["*", f"user:{user['id']}"] + [f"group:{g}" for g in user.get("groups", ())]

    def sign_principals(self, principals: list[str], now: float | None = None) -> str:
        return sign_principals(self._principal_key, principals, now)

    def retrieve(self, query: str, user: dict, k: int = 3) -> list[dict]:
        qvec = to_pgvector(embed(query))
        token = self.sign_principals(self.principals(user))
        # rag_search verifies the token, applies RLS and writes the audit row server-side (T19)
        rows = self.conn.execute(
            "SELECT id, doc_id, text, score FROM rag_search(%s, %s::vector, %s)", (token, qvec, k)
        ).fetchall()
        return [{"id": r[0], "doc_id": r[1], "text": r[2], "score": round(float(r[3]), 4)} for r in rows]

    # ── audit (hash-chained, same scheme as the JSONL backend) ───────────────
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
