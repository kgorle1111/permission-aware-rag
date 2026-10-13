"""Postgres RLS + pgvector backend tests. Skipped unless DATABASE_URL is set.

DATABASE_URL must be a privileged (schema-owning) DSN; the tests create the
non-superuser rag_app role and connect through it, so every assertion here runs
under Row-Level Security. CI provides a pgvector/pgvector:pg16 service.
"""

import json
import os
import pathlib
import time
from contextlib import contextmanager

import pytest

psycopg = pytest.importorskip("psycopg")
ADMIN = os.environ.get("DATABASE_URL")
if not ADMIN:
    pytest.skip("DATABASE_URL not set — pgvector backend not under test", allow_module_level=True)

from embedding import embed, to_pgvector  # noqa: E402
from pgvector_rag import PgVectorRAG, setup_schema  # noqa: E402

ALICE = {"id": "alice", "groups": ["eng"]}
BOB = {"id": "bob", "groups": ["hr"]}
GUEST = {"id": "guest", "groups": []}


_open: list = []


def _fresh(checkpoint=None) -> PgVectorRAG:
    while _open:  # close prior test's connection so DROP TABLE can't block on its locks
        _open.pop().close()
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename IN ('rag_app', 'rag_ingest') AND pid <> pg_backend_pid()"
        )
        c.execute("DROP TABLE IF EXISTS chunks, corpus_stats, audit, doc_meta CASCADE")
    global KEY
    KEY = setup_schema(ADMIN)
    app_dsn = psycopg.conninfo.make_conninfo(ADMIN, user="rag_app", password="rag_app")
    ingest_dsn = psycopg.conninfo.make_conninfo(ADMIN, user="rag_ingest", password="rag_ingest")
    rag = PgVectorRAG(app_dsn, ingest_dsn, principal_key=KEY, audit_checkpoint_path=checkpoint)
    _open.append(rag)
    return rag


@contextmanager
def _reader():
    """Raw SQL as the role that actually reads chunks (rag_search's NOLOGIN owner), so RLS alone
    decides. The app role itself can no longer SELECT chunks at all (T19)."""
    with psycopg.connect(ADMIN, autocommit=True) as c:
        with c.transaction():
            c.execute("SET LOCAL ROLE rag_definer")
            yield c


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

    with _reader() as c:
        # deliberately no token — raw query, RLS alone decides
        assert c.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0

    with _reader() as c:
        c.execute("SELECT set_config('rag.token', %s, true)", (rag.sign_principals(["*", "user:guest"]),))
        # even SELECT * (no app filtering) yields only the public chunk
        rows = c.execute("SELECT doc_id FROM chunks").fetchall()
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


def test_tail_deletion_is_caught_by_the_external_checkpoint(tmp_path):
    """T12: with a head checkpoint kept outside the database, deleting the newest audit rows
    (even re-chaining what is left) no longer verifies."""
    cp = tmp_path / "audit.head"
    rag = _fresh(cp)
    rag.retrieve("vacation", GUEST)  # normal operation verifies
    rag.retrieve("salary bands", BOB)
    rag.run_sql("q", "SELECT count(*) FROM doc_meta", BOB)  # the SQL path is anchored too
    assert rag.verify_audit_chain()
    with psycopg.connect(
        ADMIN, autocommit=True
    ) as c:  # admin drops the newest row: chain still self-consistent
        c.execute("DELETE FROM audit WHERE id = (SELECT max(id) FROM audit)")
    assert not rag.verify_audit_chain()
    with psycopg.connect(ADMIN, autocommit=True) as c:  # ...and then everything
        c.execute("DELETE FROM audit")
    assert not rag.verify_audit_chain()


def test_tail_deletion_with_rewritten_chain_still_fails(tmp_path):
    rag = _fresh(tmp_path / "audit.head")
    for q in ("vacation", "policy", "payments"):
        rag.retrieve(q, GUEST)
    with psycopg.connect(ADMIN, autocommit=True) as c:  # delete newest, then forge a replacement tail row
        c.execute("DELETE FROM audit WHERE id = (SELECT max(id) FROM audit)")
        prev = c.execute("SELECT line_sha256 FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0]
        line = json.dumps({"ts": 1.0, "user": "x", "returned": [], "denied_chunks": 0, "prev_sha256": prev})
        c.execute(
            "INSERT INTO audit (line, line_sha256) VALUES (%s, encode(sha256(convert_to(%s, 'UTF8')), 'hex'))",
            (line, line),
        )
    assert not rag.verify_audit_chain()


def test_missing_checkpoint_fails_closed_when_configured(tmp_path):
    cp = tmp_path / "audit.head"
    rag = _fresh(cp)
    assert rag.verify_audit_chain()  # nothing audited yet, nothing to anchor
    rag.retrieve("vacation", GUEST)
    assert rag.verify_audit_chain()
    cp.unlink()
    assert not rag.verify_audit_chain()
    cp.write_text("garbage")
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
        with _reader() as c:
            c.execute("SELECT set_config('rag.token', %s, true)", (rag.sign_principals(scope.split(",")),))
            assert {r[0] for r in c.execute("SELECT text FROM chunks")} == expected
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
    with _reader() as c:
        c.execute("SELECT set_config('rag.mode', 'ingest', true)")  # forged session setting
        c.execute("SELECT set_config('rag.token', %s, true)", (rag.sign_principals(["*", "user:guest"]),))
        assert {r[0] for r in c.execute("SELECT doc_id FROM chunks")} == {"handbook"}
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
    with _reader() as c:  # the old channel: a raw principal list is ignored now
        c.execute("SELECT set_config('rag.principals', '*,group:hr,group:exec,group:eng', true)")
        assert c.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    for forged in ("*,group:hr|9999999999|" + "0" * 64, "garbage", ""):
        with _reader() as c:
            c.execute("SELECT set_config('rag.token', %s, true)", (forged,))
            assert c.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    stale = rag.sign_principals(["*", "group:hr"], now=time.time() - 3600)
    with _reader() as c:
        c.execute("SELECT set_config('rag.token', %s, true)", (stale,))
        assert c.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with rag.conn.transaction():
            rag.conn.execute("SELECT key FROM rag_secret")
    assert docs(rag, BOB, "salary bands") == {"salaries"}  # the signed path still works


def test_app_role_can_only_search_through_the_audited_function():
    """T19: the app role can't read chunks or write audit rows directly. Every read goes through
    rag_search, which verifies the signed token, applies RLS and writes the audit row itself."""
    rag = _fresh()
    _seed(rag)
    for stmt in (
        "SELECT count(*) FROM chunks",
        "INSERT INTO audit (line, line_sha256) VALUES ('{}', 'x')",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with rag.conn.transaction():
                rag.conn.execute(stmt)
    before = len(rag.audit)
    assert docs(rag, BOB, "salary bands") == {"salaries"}
    entry = rag.audit[-1]
    assert len(rag.audit) == before + 1 and entry["user"] == "bob" and entry["returned"] == ["salaries#0"]
    forged = rag.conn.execute(
        "SELECT id FROM rag_search(%s, %s::vector, 5)",
        ("*,group:hr|1|" + "0" * 64, to_pgvector(embed("salary"))),
    ).fetchall()
    assert forged == [] and rag.audit[-1]["user"] is None and rag.audit[-1]["returned"] == []
    assert rag.verify_audit_chain()


def _app_env(cp):
    app_dsn = psycopg.conninfo.make_conninfo(ADMIN, user="rag_app", password="rag_app")
    return {
        "RAG_BACKEND": "pgvector",
        "DATABASE_URL": app_dsn,
        "RAG_PRINCIPAL_KEY": KEY.hex(),
        "AUDIT_CHECKPOINT_PATH": str(cp),
    }


def _run_py(args, env_extra):
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k not in ("RAG_BACKEND", "DATABASE_URL")}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, *args],
        cwd=pathlib.Path(__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_audit_failure_names_each_kind_of_tamper(tmp_path):
    """F5.2: verify_audit_chain stays a bool; audit_failure says which check failed (ids only)."""
    cp = tmp_path / "audit.head"
    rag = _fresh(cp)
    for q in ("vacation", "policy", "payments"):
        rag.retrieve(q, GUEST)
    assert rag.audit_failure() is None
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("UPDATE audit SET line = replace(line, 'elapsed_ms', 'elapsed_mx') WHERE id = 2")
    assert "audit row 2 does not match its recorded hash" in rag.audit_failure()
    rag = _fresh(cp)
    for q in ("vacation", "policy", "payments"):
        rag.retrieve(q, GUEST)
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM audit WHERE id = (SELECT max(id) FROM audit)")
    assert "tail rows deleted" in rag.audit_failure()
    cp.unlink()
    assert "checkpoint missing" in rag.audit_failure()


def test_cli_verifies_the_pgvector_backend(tmp_path):
    cp = tmp_path / "audit.head"
    _fresh(cp)
    rag = _open[-1]
    rag.retrieve("vacation", GUEST)
    rag.retrieve("policy", GUEST)
    env = _app_env(cp)
    ok = _run_py(["-m", "audit_verify"], env)
    assert ok.returncode == 0, ok.stderr
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM audit WHERE id = (SELECT max(id) FROM audit)")
    bad = _run_py(["-m", "audit_verify"], env)
    assert bad.returncode == 1 and "tail rows deleted" in bad.stderr
    assert KEY.hex() not in bad.stderr + bad.stdout
    cp.unlink()
    assert "checkpoint missing" in _run_py(["-m", "audit_verify"], env).stderr


def test_server_refuses_to_start_when_the_pgvector_audit_is_tampered(tmp_path):
    cp = tmp_path / "audit.head"
    rag = _fresh(cp)
    rag.retrieve("vacation", GUEST)
    rag.retrieve("policy", GUEST)
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM audit WHERE id = (SELECT max(id) FROM audit)")
    r = _run_py(["underwriter_server.py", "0"], _app_env(cp))
    assert r.returncode == 2 and "refusing to serve: audit verification failed" in r.stderr
    assert KEY.hex() not in r.stderr


def test_monitor_flips_health_on_pgvector_tamper(tmp_path):
    import audit_verify

    rag = _fresh(tmp_path / "audit.head")
    rag.retrieve("vacation", GUEST)
    monitor = audit_verify.AuditMonitor(rag, 60)
    monitor.run_once()
    assert monitor.status()["healthy"]
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM audit")
    monitor.run_once()
    assert not monitor.status()["healthy"]


def test_verify_never_races_a_concurrent_audited_read(tmp_path):
    """retrieve() commits an audit row, then rewrites the checkpoint; a verify between the two
    would report tampering on a healthy system."""
    import threading

    rag = _fresh(tmp_path / "audit.head")
    stop, bad = threading.Event(), []

    def reader():
        while not stop.is_set():
            rag.retrieve("vacation", GUEST)

    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(60):
            if (msg := rag.audit_failure()) is not None:
                bad.append(msg)
    finally:
        stop.set()
        t.join()
    assert not bad, bad[:1]
