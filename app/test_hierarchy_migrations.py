"""Upgrade populated flat schemas without widening legacy document permissions."""

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
ADMIN = os.environ.get("DATABASE_URL")
if not ADMIN:
    pytest.skip("DATABASE_URL not set — migration needs privileged Postgres", allow_module_level=True)

from embedding import DIMS  # noqa: E402
from pgvector_rag import setup_schema, sign_principals  # noqa: E402
from psycopg import sql  # noqa: E402


def test_populated_flat_schema_migrates_idempotently_and_keeps_rls():
    suffix = uuid.uuid4().hex
    database, role = f"hierarchy_migration_{suffix}", f"hierarchy_reader_{suffix}"
    dsn = psycopg.conninfo.make_conninfo(ADMIN, dbname=database)
    reader_dsn = psycopg.conninfo.make_conninfo(dsn, user=role, password="migration-local-only")
    legacy = [
        ("public#0", "public", "public handbook", ["*"]),
        ("restricted#0", "restricted", "private salary bands", ["group:hr", "user:dana"]),
    ]
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            with psycopg.connect(dsn) as conn:
                conn.execute("CREATE EXTENSION vector")
                conn.execute(
                    sql.SQL(
                        "CREATE TABLE chunks (id text PRIMARY KEY, doc_id text NOT NULL, "
                        "text text NOT NULL, acl text[] NOT NULL CHECK (cardinality(acl) > 0), "
                        "embedding vector({}) NOT NULL)"
                    ).format(sql.Literal(DIMS))
                )
                for row in legacy:
                    conn.execute(
                        "INSERT INTO chunks VALUES (%s,%s,%s,%s,%s::vector)",
                        (*row, "[" + ",".join(["0"] * DIMS) + "]"),
                    )
                conn.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
                conn.execute("ALTER TABLE chunks FORCE ROW LEVEL SECURITY")
                conn.execute(
                    "CREATE POLICY chunks_read ON chunks FOR SELECT USING "
                    "(acl && string_to_array(current_setting('rag.principals', true), ','))"
                )
            for _ in range(2):
                key = setup_schema(dsn, app_role=role, app_password="migration-local-only")
                with psycopg.connect(dsn) as conn:
                    rows = conn.execute(
                        "SELECT id, doc_id, text, acl, acl_doc, acl_section, acl_para FROM chunks ORDER BY id"
                    ).fetchall()
                    assert rows == [(*row, row[3], ["*"], ["*"]) for row in legacy]
                    nullable = conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='chunks' AND column_name IN "
                        "('acl_doc','acl_section','acl_para') "
                        "AND is_nullable='YES'"
                    ).fetchall()
                    assert nullable == []
                with psycopg.connect(reader_dsn, autocommit=True) as app_conn:
                    # T19: the app role can't read chunks directly at all
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        app_conn.execute("SELECT id FROM chunks")
                with psycopg.connect(dsn, autocommit=True) as reader:
                    # raw SQL as rag_search's owner, so RLS alone decides
                    with reader.transaction():
                        reader.execute("SET LOCAL ROLE rag_definer")
                        assert reader.execute("SELECT id FROM chunks").fetchall() == []
                    for scope, expected in [
                        ("*,user:guest", {"public#0"}),
                        ("*,user:bob,group:hr", {"public#0", "restricted#0"}),
                        ("*,user:dana", {"public#0", "restricted#0"}),
                        ("*,user:bob,group:hr-admin", {"public#0"}),
                    ]:
                        with reader.transaction():
                            reader.execute("SET LOCAL ROLE rag_definer")
                            token = sign_principals(key, scope.split(","))
                            reader.execute("SELECT set_config('rag.token', %s, true)", (token,))
                            assert {r[0] for r in reader.execute("SELECT id FROM chunks")} == expected
                    with reader.transaction():
                        reader.execute("SET LOCAL ROLE rag_definer")
                        assert reader.execute("SELECT id FROM chunks").fetchall() == []
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
