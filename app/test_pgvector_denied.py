"""`denied_chunks` on the pgvector backend: same meaning as the in-memory engine, audit-only, and cheap.

Meaning (both backends): corpus size minus the chunks the caller passes every ACL level for, i.e.
ALL hidden chunks, not "hidden among the top-k". The pgvector side computes it from `acl_groups`
(a trigger-maintained chunk count per distinct ACL triple) instead of a per-query `count(*)` over
the RLS-filtered chunks table.
"""

import random
from pathlib import Path
from typing import Any

import pytest
import test_pgvector as base
from embedding import embed, to_pgvector
from permission_rag import PermissionRAG
from pgvector_rag import PgVectorRAG
from test_pgvector import ADMIN, GUEST, psycopg

DOCS: list[tuple[str, str, list[str], list[dict[str, Any]] | None]] = [
    ("open", "vacation policy twenty days", ["*"], None),
    ("eng", "payments service uses postgres", ["group:eng"], None),
    ("hr", "salary bands ninety to two fifty", ["group:hr"], None),
    (
        "nested",  # AND across levels: doc hr, section user:bob, paragraph 2 also needs group:executive
        "",
        ["group:hr"],
        [
            {
                "acl": ["user:bob"],
                "paragraphs": [
                    {"text": "ordinary details"},
                    {"text": "secret compensation", "acl": ["group:executive"]},
                ],
            }
        ],
    ),
]
USERS: list[dict[str, Any]] = [
    {"id": "guest", "groups": []},
    {"id": "alice", "groups": ["eng"]},
    {"id": "bob", "groups": ["hr"]},
    {"id": "bob", "groups": ["hr", "executive"]},
]
# 5 chunks: open, eng, hr, nested#0, nested#1. Readable by each user above: 1, 2, 3, 4 of them.
HAND_COUNTED = [4, 3, 2, 1]


def _both(tmp_path: Path) -> tuple[PgVectorRAG, PermissionRAG]:
    pg, mem = base._fresh(), PermissionRAG(audit_path=tmp_path / "audit.jsonl")
    for doc_id, text, acl, sections in DOCS:
        pg.add_document(doc_id, text, acl, sections=sections)
        mem.add_document(doc_id, text, acl, sections=sections)
    return pg, mem


def _last_denied(rag: Any, user: dict[str, Any]) -> int:
    rag.retrieve("salary policy", user, k=3)
    return int(rag.audit[-1]["denied_chunks"])


def test_denied_count_is_hand_counted_and_equals_the_in_memory_engine(tmp_path: Path) -> None:
    pg, mem = _both(tmp_path)
    assert [_last_denied(mem, u) for u in USERS] == HAND_COUNTED
    assert [_last_denied(pg, u) for u in USERS] == HAND_COUNTED


def test_denied_count_follows_ingest_and_removal(tmp_path: Path) -> None:
    pg, _ = _both(tmp_path)
    assert _last_denied(pg, GUEST) == 4
    pg.add_document("exec", "acquisition memo", ["group:exec"])
    assert _last_denied(pg, GUEST) == 5
    pg.remove_document("hr")
    assert _last_denied(pg, GUEST) == 4
    pg.remove_document("open")
    assert _last_denied(pg, GUEST) == 4  # a readable chunk leaving the corpus is not a denial
    with psycopg.connect(ADMIN) as c:  # the groups table still agrees with the chunks table
        assert c.execute("SELECT sum(n) FROM acl_groups").fetchone()[0] == 4


def test_denied_count_matches_rls_on_random_acls() -> None:
    """Metamorphic: the group-table count equals what RLS itself says, over random ACL triples."""
    rnd = random.Random(7)
    rag = base._fresh()
    names = [f"group:g{i}" for i in range(4)] + ["user:u1", "*"]
    for i in range(60):
        sections = None
        if i % 2:
            sections = [
                {"acl": rnd.sample(names, rnd.randint(1, 3)), "paragraphs": [{"text": f"p{i}"}]},
            ]
        rag.add_document(f"d{i}", f"text {i}", rnd.sample(names, rnd.randint(1, 3)), sections=sections)
    for _ in range(25):
        user = {"id": rnd.choice(["u1", "u2"]), "groups": [f"g{j}" for j in range(4) if rnd.random() < 0.4]}
        denied = _last_denied(rag, user)
        with base._reader() as c:
            c.execute(
                "SELECT set_config('rag.token', %s, true)", (rag.sign_principals(rag.principals(user)),)
            )
            visible = c.execute("SELECT count(*) FROM chunks").fetchone()[0]
            total = c.execute("SELECT chunk_count FROM corpus_stats").fetchone()[0]
        assert denied == total - visible


def test_a_stale_group_table_falls_back_to_the_exact_count(tmp_path: Path) -> None:
    pg, _ = _both(tmp_path)
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("UPDATE acl_groups SET n = n + 100")  # drift, e.g. a maintenance path nobody wrote
    assert _last_denied(pg, GUEST) == 4


def test_callers_never_receive_the_count_or_touch_the_group_table() -> None:
    rag = base._fresh()
    base._seed(rag)
    token = rag.sign_principals(rag.principals(GUEST))
    with rag.conn.cursor() as cur:
        cur.execute("SELECT * FROM rag_search(%s, %s::vector, 3)", (token, to_pgvector(embed("vacation"))))
        assert [d.name for d in cur.description or []] == ["id", "doc_id", "text", "score"]
    assert set(rag.retrieve("vacation policy", GUEST)[0]) == {"id", "doc_id", "text", "score"}
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        rag.conn.execute("SELECT * FROM acl_groups")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        rag.conn.execute("UPDATE acl_groups SET n = 0")
