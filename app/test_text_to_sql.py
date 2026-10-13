"""Text-to-SQL under Postgres RLS (T25). Skipped unless DATABASE_URL is set (a disposable privileged DSN).

The model is always a fake. The adversarial tests bypass the app's pre-check (`check=False` or
`run_sql` directly), because the claim is that the DATABASE holds the line, not the SQL filter.
"""

import hashlib
import json
import os
import time

import pytest

psycopg = pytest.importorskip("psycopg")
ADMIN = os.environ.get("DATABASE_URL")
if not ADMIN:
    pytest.skip("DATABASE_URL not set — pgvector backend not under test", allow_module_level=True)

import eval_text_to_sql as ev  # noqa: E402
import text_to_sql  # noqa: E402
from pgvector_rag import PgVectorRAG, setup_schema  # noqa: E402

GUEST = {"id": "guest", "groups": []}
ALICE = {"id": "alice", "groups": ["eng"]}
BOB = {"id": "bob", "groups": ["hr"]}
DANA = {"id": "dana", "groups": []}
ALL = [GUEST, ALICE, BOB, DANA]
HIDDEN_MARK = "ZZTOPSECRET"  # appears only in documents/chunks that some users must never see

_open: list = []


def _fresh() -> PgVectorRAG:
    while _open:
        _open.pop().close()
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename IN ('rag_app', 'rag_ingest') AND pid <> pg_backend_pid()"
        )
        c.execute("DROP TABLE IF EXISTS chunks, corpus_stats, audit, doc_meta CASCADE")
    global KEY
    KEY = setup_schema(ADMIN)
    mk = psycopg.conninfo.make_conninfo
    rag = PgVectorRAG(
        mk(ADMIN, user="rag_app", password="rag_app"),
        mk(ADMIN, user="rag_ingest", password="rag_ingest"),
        principal_key=KEY,
    )
    _open.append(rag)
    return rag


def _meta(title, dept, cls, day):
    return {"title": title, "department": dept, "data_class": cls, "created_at": day}


def _seed(rag: PgVectorRAG) -> None:
    rag.add_document(
        "handbook",
        "Handbook: vacation is twenty days.",
        {"*"},
        meta=_meta("Handbook", "hr", "public", "2026-01-02"),
    )
    rag.add_document(
        "arch",
        "Architecture: payments use Postgres.",
        {"group:eng"},
        meta=_meta("Architecture", "eng", "internal", "2026-02-03"),
    )
    rag.add_document(
        "salaries",
        f"{HIDDEN_MARK} salary bands 90k to 250k.",
        {"group:hr"},
        meta=_meta(f"{HIDDEN_MARK} Salary bands", "hr", "confidential", "2025-05-06"),
    )
    rag.add_document(
        "merger",
        f"{HIDDEN_MARK} Acme acquisition closes.",
        {"group:exec", "user:dana"},
        meta=_meta(f"{HIDDEN_MARK} Merger memo", "exec", "restricted", "2024-07-08"),
    )


def visible_ids(rag, user) -> set[str]:
    res = rag.run_sql("visible", "SELECT doc_id FROM doc_meta", user)
    assert res["status"] == "ok"
    return {r["doc_id"] for r in res["rows"]}


EXPECT = {
    "guest": {"handbook"},
    "alice": {"handbook", "arch"},
    "bob": {"handbook", "salaries"},
    "dana": {"handbook", "merger"},
}


def test_honest_query_sees_exactly_what_rls_admits():
    rag = _fresh()
    _seed(rag)
    for u in ALL:
        assert visible_ids(rag, u) == EXPECT[u["id"]], u["id"]


@pytest.fixture(scope="module")
def seeded():
    """One seeded database for the read-only attack cases (each case re-checks that nothing changed)."""
    rag = _fresh()
    _seed(rag)
    return rag


TITLES = {
    "handbook": "Handbook",
    "arch": "Architecture",
    "salaries": f"{HIDDEN_MARK} Salary bands",
    "merger": f"{HIDDEN_MARK} Merger memo",
}


def _no_hidden_values(res: dict, user: dict, rag: PgVectorRAG) -> None:
    """The attack either errored or returned nothing the caller couldn't read via the honest query."""
    blob = json.dumps(res)
    for doc in set(TITLES) - visible_ids(rag, user):
        assert doc not in blob and TITLES[doc] not in blob, (doc, blob)


@pytest.mark.parametrize("user", ALL, ids=lambda u: u["id"])
@pytest.mark.parametrize("name,sql", ev.ATTACKS, ids=[n for n, _ in ev.ATTACKS])
def test_hostile_sql_errors_or_returns_only_visible_rows(seeded, name, sql, user):
    rag = seeded
    t0 = time.monotonic()
    res = text_to_sql.answer(rag, name, user, lambda _q: sql, check=False)  # pre-check OFF
    assert time.monotonic() - t0 < 5, "statement_timeout did not bound the statement"
    assert res["status"] in ("ok", "error"), res
    _no_hidden_values(res, user, rag)
    with psycopg.connect(ADMIN, autocommit=True) as c:  # one statement never changes anything
        assert c.execute("SELECT count(*) FROM doc_meta").fetchone()[0] == 4
        assert c.execute("SELECT count(*) FROM chunks").fetchone()[0] == 4
        assert c.execute("SELECT to_regclass('loot')").fetchone()[0] is None
        assert c.execute("SELECT count(*) FROM pg_largeobject_metadata").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'evil'").fetchone()[0] == 0


# every hostile statement that touches something the role may not: a privilege/structure error
MUST_ERROR = {
    "all chunks", "chunk text via subquery", "chunks via CTE", "chunks via UNION", "signing key", "key via UNION",
    "audit table", "ACL column", "star over ACL column", "SET ROLE postgres", "SET ROLE ingest", "RESET ROLE",
    "set_config role", "set_config session_authorization", "stacked DROP", "DROP TABLE", "DELETE", "UPDATE ACL",
    "INSERT", "TRUNCATE", "CREATE TABLE", "SELECT INTO", "data-modifying CTE", "GRANT", "CREATE ROLE",
    "COPY to file", "COPY to program", "read server file", "list server dir", "import server file",
    "write large object",
}  # fmt: skip
SLOW = {"sleep past the timeout", "cartesian blow-up"}  # both end at the 2 s timeout; checked once below


def test_dangerous_statements_error_for_every_user():
    rag = _fresh()
    _seed(rag)
    assert {n for n, _ in ev.ATTACKS} >= MUST_ERROR | SLOW
    for name, sql in ev.ATTACKS:
        if name in MUST_ERROR:
            for u in ALL:
                res = text_to_sql.answer(rag, name, u, lambda _q, s=sql: s, check=False)
                assert res["status"] == "error", (name, u["id"], res)
    for name, sql in ev.ATTACKS:
        if name in SLOW:
            res = text_to_sql.answer(rag, name, BOB, lambda _q, s=sql: s, check=False)
            assert res == {"sql": sql, "rows": [], "truncated": False, "status": "error", "sqlstate": "57014"}


def test_aggregates_count_only_visible_rows():
    """T15 for metadata: count/sum/min/group-by over doc_meta see exactly the visible rows."""
    rag = _fresh()
    _seed(rag)
    q = "SELECT count(*) AS n, sum(n_chunks) AS c, min(created_at) AS first, count(DISTINCT department) AS d FROM doc_meta"
    want = {
        "guest": {"n": 1, "c": 1, "first": "2026-01-02", "d": 1},
        "alice": {"n": 2, "c": 2, "first": "2026-01-02", "d": 2},
        "bob": {"n": 2, "c": 2, "first": "2025-05-06", "d": 1},
        "dana": {"n": 2, "c": 2, "first": "2024-07-08", "d": 2},
    }
    for u in ALL:
        res = rag.run_sql("agg", q, u)
        assert res["status"] == "ok" and res["rows"] == [want[u["id"]]], (u["id"], res)
    res = rag.run_sql(
        "groups", "SELECT department, count(*) AS n FROM doc_meta GROUP BY department ORDER BY 1", GUEST
    )
    assert res["rows"] == [{"department": "hr", "n": 1}]
    res = rag.run_sql(
        "hidden class", "SELECT count(*) AS n FROM doc_meta WHERE data_class = 'restricted'", GUEST
    )
    assert res["rows"] == [{"n": 0}]


def test_error_message_oracle_on_hidden_rows_does_not_fire():
    """RLS filters before user expressions run, so `1/0` on a hidden row's value is never evaluated."""
    rag = _fresh()
    _seed(rag)
    probe = f"SELECT count(*) AS n FROM doc_meta WHERE (1 / (length(title) - length('{HIDDEN_MARK} Merger memo'))) IS NOT NULL"
    guest = rag.run_sql("oracle", probe, GUEST)
    assert guest == {"status": "ok", "rows": [{"n": 1}], "truncated": False}
    dana = rag.run_sql("oracle", probe, DANA)  # she may read it, so the division by zero is real
    assert dana["status"] == "error" and dana["sqlstate"] == "22012"


def test_forged_or_stale_principals_inside_the_statement_widen_nothing():
    rag = _fresh()
    _seed(rag)
    stale = rag.sign_principals(
        ["*", "group:exec", "group:hr"], now=time.time() - 3600
    )  # real signature, expired
    fresh_unsigned = f"*,group:exec,group:hr|{int(time.time())}|{'0' * 64}"
    for tok in (stale, fresh_unsigned, "", "garbage"):
        swap = f"SELECT set_config('rag.token', '{tok}', true) IS NOT NULL AS swapped, count(*) AS n FROM doc_meta"
        res = rag.run_sql("swap", swap, GUEST)
        assert res["status"] == "ok" and res["rows"][0]["n"] <= 1, (tok, res)
        # swap first, read second: a CTE that must finish before the main query reads
        res = rag.run_sql(
            "swap2",
            f"WITH s AS MATERIALIZED (SELECT set_config('rag.token', '{tok}', true)) "
            "SELECT count(*) AS n FROM doc_meta, s",
            GUEST,
        )
        assert res["status"] == "ok" and res["rows"][0]["n"] <= 1, (tok, res)
    res = rag.run_sql(
        "raw",
        "SELECT set_config('rag.principals', '*,group:exec,group:hr', true), count(*) AS n FROM doc_meta",
        GUEST,
    )
    assert res["rows"][0]["n"] == 1


def test_database_blocks_hostile_sql_even_if_the_precheck_is_bypassed():
    rag = _fresh()
    _seed(rag)
    stacked = "SELECT 1; DROP TABLE doc_meta"
    with pytest.raises(text_to_sql.SqlRejected):
        text_to_sql.precheck(stacked)
    assert text_to_sql.answer(rag, "q", BOB, lambda _q: stacked)["status"] == "rejected"
    bypassed = text_to_sql.answer(rag, "q", BOB, lambda _q: stacked, check=False)
    assert bypassed["status"] == "error"  # the cursor refuses multi-statement text
    assert visible_ids(rag, BOB) == EXPECT["bob"]  # table intact either way


def test_precheck_accepts_one_select_and_refuses_the_rest():
    assert text_to_sql.precheck(" SELECT 1 ;") == "SELECT 1"
    assert text_to_sql.precheck("with a as (select 1) select * from a").startswith("with")
    for bad in [
        "",
        "   ",
        "DELETE FROM doc_meta",
        "SELECT 1; SELECT 2",
        "SELECT 1 -- x",
        "SELECT /* x */ 1",
        "x" * 2001,
        None,
        "SET ROLE x",
    ]:
        with pytest.raises(text_to_sql.SqlRejected):
            text_to_sql.precheck(bad)


def test_roles_are_least_privilege_and_not_bypassing_rls():
    rag = _fresh()
    with psycopg.connect(ADMIN, autocommit=True) as c:
        sup, byp, login = c.execute(
            "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = 'rag_sql'"
        ).fetchone()
        assert (sup, byp, login) == (False, False, False)
        owner = c.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'doc_meta'"
        ).fetchone()[0]
        assert owner != "rag_sql"
        assert c.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'doc_meta'").fetchone()[0]
        assert (
            c.execute("SELECT count(*) FROM pg_auth_members WHERE roleid = 'rag_sql'::regrole").fetchone()[0]
            == 0
        )
        for t in ("chunks", "audit", "rag_secret", "corpus_stats"):
            assert not c.execute("SELECT has_table_privilege('rag_sql', %s, 'SELECT')", (t,)).fetchone()[0], t
        for priv in ("INSERT", "UPDATE", "DELETE"):
            assert not c.execute("SELECT has_table_privilege('rag_sql', 'doc_meta', %s)", (priv,)).fetchone()[
                0
            ]
        assert not c.execute(
            "SELECT has_column_privilege('rag_sql', 'doc_meta', 'acl_doc', 'SELECT')"
        ).fetchone()[0]
        assert not c.execute("SELECT has_schema_privilege('rag_sql', 'public', 'CREATE')").fetchone()[0]
        fn = "rag_sql_exec(text, integer)"
        assert not c.execute("SELECT has_function_privilege('rag_app', %s, 'EXECUTE')", (fn,)).fetchone()[0]
    # the app role cannot become rag_sql, read doc_meta, or call the executor directly
    for stmt in ("SET ROLE rag_sql", "SELECT * FROM doc_meta", "SELECT rag_sql_exec('SELECT 1', 5)"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with rag.conn.transaction():
                rag.conn.execute(stmt)


def test_every_attempt_is_audited_by_the_database_without_text():
    rag = _fresh()
    _seed(rag)
    question = "how many zzprivate-applicant-4242 documents?"
    sql = "SELECT count(*) AS n FROM doc_meta WHERE title <> 'zzprivate-applicant-4242'"
    before = len(rag.audit)
    assert text_to_sql.answer(rag, question, BOB, lambda _q: sql)["rows"] == [{"n": 2}]
    text_to_sql.answer(rag, question, BOB, lambda _q: "DROP TABLE doc_meta")  # rejected by the pre-check
    text_to_sql.answer(rag, question, BOB, lambda _q: "SELECT * FROM chunks", check=False)  # privilege error
    text_to_sql.answer(rag, question, BOB, lambda _q: "SELECT pg_sleep(30)", check=False)  # timeout
    rows = rag.audit[before:]
    assert [(e["kind"], e["user"], e["status"]) for e in rows] == [
        ("sql", "bob", "ok"), ("sql", "bob", "rejected"), ("sql", "bob", "error"), ("sql", "bob", "error"),
    ]  # fmt: skip
    assert [e["sqlstate"] for e in rows] == [None, None, "42501", "57014"]
    assert (
        rows[0]["returned_rows"] == 1
        and rows[0]["question_sha256"] == hashlib.sha256(question.encode()).hexdigest()
    )
    assert rows[0]["sql_sha256"] == hashlib.sha256(sql.encode()).hexdigest()
    assert rows[1]["sql_sha256"] == hashlib.sha256(b"DROP TABLE doc_meta").hexdigest()
    with psycopg.connect(ADMIN) as c:  # invariant 9: no question or SQL text at rest
        lines = " ".join(r[0] for r in c.execute("SELECT line FROM audit"))
    assert "zzprivate-applicant-4242" not in lines and "doc_meta" not in lines
    assert rag.verify_audit_chain()


def test_app_role_cannot_forge_sql_audit_rows_or_skip_them():
    """T19 mirror: the only way to run a statement is rag_sql_run, which writes the row itself."""
    rag = _fresh()
    _seed(rag)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with rag.conn.transaction():
            rag.conn.execute("INSERT INTO audit (line, line_sha256) VALUES ('{}', 'x')")
    forged = rag.conn.execute(
        "SELECT rag_sql_run(%s, 'q', 'SELECT count(*) FROM doc_meta', 5)", ("*,group:hr|1|" + "0" * 64,)
    ).fetchone()[0]
    assert forged["rows"] == [{"count": 0}]  # a forged token is a guest with nothing, not an error
    last = rag.audit[-1]
    assert last["user"] is None and last["kind"] == "sql" and last["returned_rows"] == 1
    assert rag.verify_audit_chain()


def test_session_advisory_lock_from_the_statement_cannot_stall_the_audit_chain():
    rag = _fresh()
    _seed(rag)
    res = rag.run_sql("lock", "SELECT pg_advisory_lock(hashtext('rag_audit'))", BOB)
    assert res["status"] == "ok"  # the statement ran...
    other = PgVectorRAG(
        psycopg.conninfo.make_conninfo(ADMIN, user="rag_app", password="rag_app"), principal_key=KEY
    )
    _open.append(other)
    other.conn.execute("SET statement_timeout = 3000")
    t0 = time.monotonic()
    other.retrieve("vacation", GUEST)  # ...and this audited call, on another connection, is not blocked
    assert time.monotonic() - t0 < 2.5


def test_row_cap_and_large_result_are_bounded():
    rag = _fresh()
    res = rag.run_sql("big", "SELECT generate_series(1, 100000) AS g", GUEST)
    assert res["status"] == "ok" and len(res["rows"]) == PgVectorRAG.SQL_MAX_ROWS and res["truncated"] is True
    one = rag.run_sql("one", "SELECT generate_series(1, 100) AS g", GUEST)
    assert len(one["rows"]) == 100 and one["truncated"] is False  # exactly at the cap is not truncated


def test_metamorphic_hidden_rows_never_change_a_low_privilege_result():
    """Adding documents nobody in `users` can read must not change any of their results."""
    rag = _fresh()
    _seed(rag)
    questions = [sql for _q, sql, _g in ev.Q] + [
        "SELECT count(*) AS n, avg(n_chunks) AS a, max(created_at) AS m FROM doc_meta",
        "SELECT * FROM (SELECT doc_id FROM doc_meta ORDER BY created_at LIMIT 1) t",
        "SELECT department, count(*) AS n FROM doc_meta GROUP BY ROLLUP (department) ORDER BY 1",
        "SELECT doc_id, row_number() OVER (ORDER BY created_at) AS r FROM doc_meta ORDER BY 1",
    ]

    def snapshot():
        return {(u["id"], q): rag.run_sql("m", q, u) for u in (GUEST, ALICE) for q in questions}

    before = snapshot()
    for i in range(5):  # earliest/latest dates, new departments, many chunks: every aggregate would move
        rag.add_document(
            f"board-{i}", ("board only text. " * 200), {"group:board"},
            meta=_meta(f"Board {i}", f"dept-{i}", "board", f"{1990 + i}-01-01" if i % 2 else "2999-12-31"),
        )  # fmt: skip
    with psycopg.connect(ADMIN) as c:  # control: the hidden rows really were added
        assert c.execute("SELECT count(*) FROM doc_meta").fetchone()[0] == 9
    after = snapshot()
    assert after == before
    assert rag.remove_document("board-0") > 0
    assert snapshot() == before
    assert visible_ids(rag, GUEST) == {"handbook"}


def test_ingest_stores_metadata_and_only_the_ingest_role_writes_it():
    rag = _fresh()
    _seed(rag)
    assert rag.remove_document("arch") == 1
    assert "arch" not in visible_ids(rag, ALICE)
    for kw in ({"bogus": 1}, {"created_at": 5}, {"title": 7}, {"department": "x" * 201}):
        with pytest.raises((ValueError, TypeError)):
            rag.add_document("bad", "text", {"*"}, meta=kw)
    with pytest.raises(ValueError):
        rag.add_document("bad2", "text", {"*"}, meta={"created_at": "not-a-date"})
    for stmt in (
        "INSERT INTO doc_meta (doc_id, title, n_chunks, acl_doc) VALUES ('x','x',0,ARRAY['*'])",
        "DELETE FROM doc_meta",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with rag.conn.transaction():
                rag.conn.execute(stmt)
    assert visible_ids(rag, GUEST) == {"handbook"}  # failed ingests left no metadata row behind


def test_metadata_visibility_is_document_level_only_documented_residual():
    """T25 residual: doc_meta uses the document-level ACL, so a public document whose every
    section is restricted still lists (title, department...) for a caller who can read no chunk."""
    rag = _fresh()
    rag.add_document(
        "nested",
        "",
        ["*"],
        sections=[{"acl": ["group:hr"], "text": "section text"}],
        meta=_meta("Nested", "hr", "x", "2026-01-01"),
    )
    assert visible_ids(rag, GUEST) == {"nested"}
    rag.retrieve("section text", GUEST)
    assert rag.audit[-1]["returned"] == []


def test_catalog_row_estimates_are_a_documented_residual_count_leak():
    """T25 residual: PUBLIC can read pg_class.reltuples, which is the TABLE's row estimate, hidden rows
    included. RLS cannot hide it. If this ever stops being true, update T25 and the README section."""
    rag = _fresh()
    _seed(rag)
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("ANALYZE doc_meta")
    res = rag.run_sql("est", "SELECT reltuples::int AS n FROM pg_class WHERE relname = 'doc_meta'", GUEST)
    assert res["rows"] == [{"n": 4}]  # guest is entitled to 1
    assert rag.run_sql("stats", "SELECT * FROM pg_stats WHERE tablename = 'doc_meta'", GUEST)["rows"] == []


def test_existing_chunks_are_backfilled_into_doc_meta_on_setup():
    rag = _fresh()
    _seed(rag)
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DELETE FROM doc_meta")  # a database ingested before doc_meta existed
    assert visible_ids(rag, DANA) == set()
    setup_schema(ADMIN)
    assert visible_ids(rag, DANA) == EXPECT["dana"]
    assert visible_ids(rag, GUEST) == EXPECT["guest"]
    assert rag.run_sql("t", "SELECT title FROM doc_meta ORDER BY 1", GUEST)["rows"] == [{"title": "handbook"}]


# ── the suite must be able to fail: plant each weakness and watch the invariant break ────────────
def _guest_count(rag) -> int:
    return rag.run_sql("c", "SELECT count(*) AS n FROM doc_meta", GUEST)["rows"][0]["n"]


@pytest.mark.parametrize(
    "plant",
    [
        "ALTER ROLE rag_sql BYPASSRLS",
        "GRANT CREATE ON SCHEMA public TO rag_sql; ALTER TABLE doc_meta NO FORCE ROW LEVEL SECURITY; "
        "ALTER TABLE doc_meta OWNER TO rag_sql",
        "DROP POLICY doc_meta_read ON doc_meta; CREATE POLICY doc_meta_read ON doc_meta FOR SELECT USING (true)",
    ],
    ids=["bypassrls", "owner-without-force", "open-policy"],
)
def test_planted_weaknesses_are_caught_by_the_visible_only_invariant(plant):
    rag = _fresh()
    _seed(rag)
    assert _guest_count(rag) == 1
    with psycopg.connect(ADMIN, autocommit=True) as c:
        for stmt in plant.split("; "):
            c.execute(stmt)
    try:
        assert _guest_count(rag) == 4, "planted weakness should expose hidden rows to the guest"
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute("ALTER TABLE doc_meta OWNER TO CURRENT_USER")
        setup_schema(ADMIN)  # setup restores policy, FORCE RLS, NOBYPASSRLS and grants
    assert _guest_count(rag) == 1


def test_planted_acl_column_grant_is_caught():
    rag = _fresh()
    _seed(rag)
    assert rag.run_sql("a", "SELECT acl_doc FROM doc_meta", GUEST)["status"] == "error"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("GRANT SELECT (acl_doc) ON doc_meta TO rag_sql")
    try:
        assert rag.run_sql("a", "SELECT acl_doc FROM doc_meta", GUEST)["status"] == "ok"
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute("REVOKE SELECT (acl_doc) ON doc_meta FROM rag_sql")


def test_optional_real_model_adapter_builds_a_request_without_the_network(monkeypatch):
    import llm

    seen = {}

    def fake_post(body, key, timeout):
        seen.update(body=body, key=key)
        return {"content": [{"text": "SELECT 1"}]}

    monkeypatch.setattr(llm, "_post", fake_post)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        text_to_sql.anthropic_llm("q")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert text_to_sql.anthropic_llm("how many?") == "SELECT 1"
    assert seen["body"]["system"] == text_to_sql.SCHEMA_PROMPT
    assert seen["body"]["messages"] == [{"role": "user", "content": "how many?"}]
