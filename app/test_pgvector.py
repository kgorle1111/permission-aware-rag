"""Postgres RLS + pgvector backend tests. Skipped unless DATABASE_URL is set.

DATABASE_URL must be a privileged (schema-owning) DSN; the tests create the
non-superuser rag_app role and connect through it, so every assertion here runs
under Row-Level Security. CI provides a pgvector/pgvector:pg16 service.
"""

import json
import os
import pathlib

import pytest

psycopg = pytest.importorskip("psycopg")
ADMIN = os.environ.get("DATABASE_URL")
if not ADMIN:
    pytest.skip("DATABASE_URL not set — pgvector backend not under test", allow_module_level=True)

from pgvector_rag import PgVectorRAG, setup_schema  # noqa: E402

ALICE = {"id": "alice", "groups": ["eng"]}
BOB = {"id": "bob", "groups": ["hr"]}
GUEST = {"id": "guest", "groups": []}


def _fresh() -> PgVectorRAG:
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS chunks, corpus_stats, audit CASCADE")
    setup_schema(ADMIN)
    app_dsn = psycopg.conninfo.make_conninfo(ADMIN, user="rag_app", password="rag_app")
    return PgVectorRAG(app_dsn)


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
        rag.conn.execute("SELECT set_config('rag.principals', '*,user:guest', true)")
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
        c.execute(
            "UPDATE audit SET line = replace(line, 'vacation', 'salaries') WHERE line LIKE '%vacation%'"
        )
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
