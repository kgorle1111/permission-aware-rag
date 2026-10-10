"""Postgres RLS + pgvector backend tests. Skipped unless DATABASE_URL is set.

DATABASE_URL must be a privileged (schema-owning) DSN; the tests create the
non-superuser rag_app role and connect through it, so every assertion here runs
under Row-Level Security. CI provides a pgvector/pgvector:pg16 service.
"""

import json
import os
import pathlib
import time

import pytest

psycopg = pytest.importorskip("psycopg")
ADMIN = os.environ.get("DATABASE_URL")
if not ADMIN:
    pytest.skip("DATABASE_URL not set — pgvector backend not under test", allow_module_level=True)

from pgvector_rag import PgVectorRAG, setup_schema  # noqa: E402

ALICE = {"id": "alice", "groups": ["eng"]}
BOB = {"id": "bob", "groups": ["hr"]}
GUEST = {"id": "guest", "groups": []}


_open: list = []


def _fresh() -> PgVectorRAG:
    while _open:  # close prior test's connection so DROP TABLE can't block on its locks
        _open.pop().close()
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename IN ('rag_app', 'rag_ingest') AND pid <> pg_backend_pid()"
        )
        c.execute("DROP TABLE IF EXISTS chunks, corpus_stats, audit CASCADE")
    global KEY
    KEY = setup_schema(ADMIN)
    app_dsn = psycopg.conninfo.make_conninfo(ADMIN, user="rag_app", password="rag_app")
    ingest_dsn = psycopg.conninfo.make_conninfo(ADMIN, user="rag_ingest", password="rag_ingest")
    rag = PgVectorRAG(app_dsn, ingest_dsn, principal_key=KEY)
    _open.append(rag)
    return rag


def _seed(rag: PgVectorRAG) -> None:
    rag.add_document("handbook", "Company handbook: vacation policy is twenty days per year.", {"*"})
    rag.add_document("arch", "Engineering architecture: the payments service uses Postgres.", {"group:eng"})
    rag.add_document("salaries", "HR confidential: salary bands range from 90k to 250k.", {"group:hr"})
    rag.add_document(
        "merger", "Executive memo: the Acme acquisition closes next quarter.", {"group:exec", "user:dana"}
    )


def docs(rag, user, q, k=5):
    return {r["doc_id"] for r in rag.retrieve(q, user, k=k)}


def test_acl_visibility_and_leaks():
    rag = _fresh()
    _seed(rag)

    # public doc visible to everyone
    for u in (ALICE, BOB, GUEST):
        assert "handbook" in docs(rag, u, "vacation policy"), u["id"]

    # group scoping
    assert "arch" in docs(rag, ALICE, "payments postgres")
    assert "arch" not in docs(rag, BOB, "payments postgres")
    assert "salaries" in docs(rag, BOB, "salary bands")

    # THE leak test: exact-content queries never return a forbidden doc
    assert "merger" not in docs(rag, ALICE, "Acme acquisition closes next quarter")
    assert "salaries" not in docs(rag, GUEST, "salary bands 90k 250k")

    # user-level ACL entry works
    assert "merger" in docs(rag, {"id": "dana", "groups": []}, "Acme acquisition")

    # audit recorded denied counts (guest sees 1 of 4 chunks)
    entry = rag.audit[-1]
    assert entry["user"] == "dana" and "denied_chunks" in entry
    guest_entry = [e for e in rag.audit if e["user"] == "guest"][-1]
    assert guest_entry["denied_chunks"] == 3


def test_rls_is_the_enforcer_not_the_app():
    """The security property lives in Postgres: with no principals GUC set, the
    app role sees an EMPTY table — no WHERE clause in our code is involved."""
    rag = _fresh()
    _seed(rag)

    with rag.conn.transaction():
        # deliberately no set_config — raw query as the app role
        assert rag.conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0

    with rag.conn.transaction():
        rag.conn.execute(
            "SELECT set_config('rag.token', %s, true)", (rag.sign_principals(["*", "user:guest"]),)
        )
        # even SELECT * (no app filtering) yields only the public chunk
        rows = rag.conn.execute("SELECT doc_id FROM chunks").fetchall()
        assert {r[0] for r in rows} == {"handbook"}


def test_ingest_guards_and_remove():
    rag = _fresh()
    _seed(rag)
    with pytest.raises(ValueError):
        rag.add_document("bad", "text", set())
    with pytest.raises(ValueError):
        rag.add_document("handbook", "again", {"*"})
    assert rag.remove_document("handbook") == 1
    assert rag.remove_document("missing") == 0
    rag.add_document("handbook", "replacement handbook text.", {"*"})
    assert "handbook" in docs(rag, GUEST, "replacement handbook")


def test_audit_hash_chain_tamper_detection():
    rag = _fresh()
    _seed(rag)
    rag.retrieve("vacation", GUEST)
    rag.retrieve("salary bands", BOB)
    assert rag.verify_audit_chain()
    with psycopg.connect(ADMIN, autocommit=True) as c:  # attacker with raw DB access edits a row
        c.execute("UPDATE audit SET line = replace(line, 'elapsed_ms', 'elapsed_mx')")
    assert not rag.verify_audit_chain()


def test_newest_audit_entry_tamper():
    rag = _fresh()
    rag.retrieve("vacation", GUEST)
    assert rag.verify_audit_chain()
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("UPDATE audit SET line = replace(line, 'elapsed_ms', 'elapsed_mx')")
    assert not rag.verify_audit_chain()


def test_underwriter_eval_leak_gate():
    """Same 20-case leak gate as the in-memory backend, over RLS + pgvector."""
    from underwriter_server import CORPUS, USERS

    rag = _fresh()
    for doc_id, text, acl in CORPUS:
        rag.add_document(doc_id, text, acl)

    cases = json.loads((pathlib.Path(__file__).with_name("evals.json")).read_text())
    leaks, recall_hits, recall_total = [], 0, 0
    for case in cases:
        got = docs(rag, USERS[case["role"]], case["q"], k=4)
        for forbidden in case["must_not"]:
            if forbidden in got:
                leaks.append((case["role"], case["q"], forbidden))
        for want in case["expect"]:
            recall_total += 1
            recall_hits += want in got
    assert not leaks, leaks  # the product's core claim — must always hold
    # hashed embeddings are a placeholder; require recall to stay useful, not perfect
    assert recall_hits >= recall_total * 0.8, f"recall {recall_hits}/{recall_total}"


def test_acl_and_principal_boundary():
    rag = _fresh()
    with pytest.raises(TypeError):
        rag.add_document("s", "text", "group:hr")
    with pytest.raises(ValueError):
        rag.add_document("s", "text", {"group:a,b"})
    with pytest.raises(ValueError):
        PgVectorRAG.principals({"id": "u", "groups": ["eng,group:hr"]})
    with pytest.raises(ValueError):
        PgVectorRAG.principals({"id": "", "groups": []})


def test_hierarchy_rls_checks_every_level_without_application_filter():
    rag = _fresh()
    rag.add_document(
        "nested",
        "",
        ["group:hr"],
        sections=[
            {
                "acl": ["user:bob"],
                "paragraphs": [
                    {"text": "salary policy ordinary details"},
                    {"text": "salary policy secret compensation", "acl": ["group:executive"]},
                ],
            },
        ],
    )
    for scope, expected in [
        ("*,user:bob,group:hr", {"salary policy ordinary details"}),
        ("*,user:bob", set()),
        ("*,user:alice,group:hr", set()),
        (
            "*,user:bob,group:hr,group:executive",
            {"salary policy ordinary details", "salary policy secret compensation"},
        ),
    ]:
        with rag.conn.transaction():
            rag.conn.execute(
                "SELECT set_config('rag.token', %s, true)", (rag.sign_principals(scope.split(",")),)
            )
            assert {r[0] for r in rag.conn.execute("SELECT text FROM chunks")} == expected
    with pytest.raises((TypeError, ValueError)):
        rag.add_document("invalid", "lead", ["*"], sections=[{"text": "secret", "acl": []}])
    with psycopg.connect(ADMIN) as c:
        assert c.execute("SELECT count(*) FROM chunks WHERE doc_id='invalid'").fetchone()[0] == 0


def test_new_audit_omits_query_text_from_persisted_lines():
    rag = _fresh()
    _seed(rag)
    rag.retrieve("vacation private-applicant-8675309", GUEST)
    assert rag.audit[0]["query"] == "[redacted]"
    assert rag.audit[0]["returned"]
    with psycopg.connect(ADMIN) as conn:
        line = conn.execute("SELECT line FROM audit").fetchone()[0]
    assert "private-applicant-8675309" not in line
    assert rag.verify_audit_chain()


def test_app_role_cannot_ingest_or_widen_visibility():
    """T13: ingest is a separate DB role. Session settings the app role can forge must not
    grant write access or the ingest policy's read-everything visibility."""
    rag = _fresh()
    _seed(rag)
    with psycopg.connect(ADMIN, autocommit=True) as c:  # a database set up before T13
        c.execute("GRANT INSERT, UPDATE, DELETE ON chunks TO rag_app")
    setup_schema(ADMIN)  # re-running setup must migrate the legacy write grants away
    with rag.conn.transaction():
        rag.conn.execute("SELECT set_config('rag.mode', 'ingest', true)")  # forged by the app role
        rag.conn.execute(
            "SELECT set_config('rag.token', %s, true)", (rag.sign_principals(["*", "user:guest"]),)
        )
        assert {r[0] for r in rag.conn.execute("SELECT doc_id FROM chunks")} == {"handbook"}
    for stmt in (
        "INSERT INTO chunks (id, doc_id, text, acl, acl_doc, acl_section, acl_para, embedding) "
        "SELECT 'x#0', 'x', 'x', ARRAY['*'], ARRAY['*'], ARRAY['*'], ARRAY['*'], embedding FROM chunks LIMIT 1",
        "DELETE FROM chunks",
        "SET ROLE rag_ingest",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with rag.conn.transaction():
                rag.conn.execute(stmt)
    reader = PgVectorRAG(
        psycopg.conninfo.make_conninfo(ADMIN, user="rag_app", password="rag_app"), principal_key=KEY
    )
    _open.append(reader)
    with pytest.raises(PermissionError):
        reader.add_document("new", "text", {"*"})
    with pytest.raises(PermissionError):
        reader.remove_document("handbook")


def test_forged_principals_cannot_widen_visibility():
    """T18: SQL on the app connection can set any session setting, but principals only reach
    RLS as an HMAC-signed, fresh token verified inside Postgres with a key the app role can't read."""
    rag = _fresh()
    _seed(rag)
    with rag.conn.transaction():  # the old channel: a raw principal list is ignored now
        rag.conn.execute("SELECT set_config('rag.principals', '*,group:hr,group:exec,group:eng', true)")
        assert rag.conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    for forged in ("*,group:hr|9999999999|" + "0" * 64, "garbage", ""):
        with rag.conn.transaction():
            rag.conn.execute("SELECT set_config('rag.token', %s, true)", (forged,))
            assert rag.conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    stale = rag.sign_principals(["*", "group:hr"], now=time.time() - 3600)
    with rag.conn.transaction():
        rag.conn.execute("SELECT set_config('rag.token', %s, true)", (stale,))
        assert rag.conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with rag.conn.transaction():
            rag.conn.execute("SELECT key FROM rag_secret")
    assert docs(rag, BOB, "salary bands") == {"salaries"}  # the signed path still works
