"""Text-to-SQL eval (ROADMAP 11.3): exact-match accuracy and leaks, over Postgres RLS, with a FAKE model.

Run (needs a DISPOSABLE Postgres; it drops and recreates the rag tables):
    DATABASE_URL=postgresql://postgres:...@host:port/db python3 eval_text_to_sql.py [out_dir]

The "model" is a lookup table (question -> SQL), so this measures the database path and the
permission guarantee, not any LLM's SQL quality; no paid call is made. Gold answers come from an
independent oracle: plain Python over the metadata rows the in-memory `PermissionRAG.can_read`
says the user may read. A leak is any hidden document's id or title in any result.
"""

import json
import os
import pathlib
import sys
from collections import Counter

import psycopg
import underwriter_server as srv
from permission_rag import PermissionRAG
from pgvector_rag import PgVectorRAG, setup_schema
from run_evals import TRAPS
from text_to_sql import answer

USERS = {**srv.USERS, "guest": {"id": "guest", "groups": []}}

# doc_id -> (title, department, data_class, created_at); the corpus text/ACLs come from the demo app
META = {
    "policy-10023": ("Homeowners policy 10023", "underwriting", "policy", "2026-03-02"),
    "policy-10088": ("Commercial policy 10088", "underwriting", "policy", "2026-04-15"),
    "claims-10023": ("Claims history 10023", "claims", "claims", "2024-03-20"),
    "bank-delgado": ("Delgado bank profile", "banking", "financial", "2026-05-01"),
    "credit-memo-delgado": ("Delgado credit memo", "credit", "credit", "2026-06-10"),
    "watchlist": ("Compliance watchlist", "compliance", "compliance", "2025-11-03"),
    "guidelines": ("Underwriting guidelines", "underwriting", "guideline", "2025-01-10"),
    "trap-admin": ("Underwriting admin overrides", "underwriting", "policy", "2026-07-01"),
    "trap-star": ("Banking star partners", "banking", "financial", "2026-07-02"),
}
DOCS = srv.CORPUS + TRAPS

# (question, SQL the fake model returns, gold(visible rows) -> rows). Rows are dicts: jsonb has no column order.
Q = [
    ("How many documents can I read?", "SELECT count(*) AS n FROM doc_meta", lambda v: [{"n": len(v)}]),
    (
        "List the titles I can read, alphabetically.",
        "SELECT title FROM doc_meta ORDER BY title",
        lambda v: [{"title": r["title"]} for r in sorted(v, key=lambda r: r["title"])],
    ),
    (
        "How many documents per department?",
        "SELECT department, count(*) AS n FROM doc_meta GROUP BY department ORDER BY department",
        lambda v: [{"department": d, "n": n} for d, n in sorted(Counter(r["department"] for r in v).items())],
    ),
    (
        "Which compliance documents can I read?",
        "SELECT doc_id FROM doc_meta WHERE department = 'compliance' ORDER BY doc_id",
        lambda v: [
            {"doc_id": r["doc_id"]}
            for r in sorted(v, key=lambda r: r["doc_id"])
            if r["department"] == "compliance"
        ],
    ),
    (
        "What is the most recent document I can read?",
        "SELECT doc_id FROM doc_meta ORDER BY created_at DESC, doc_id DESC LIMIT 1",
        lambda v: (
            [{"doc_id": sorted(v, key=lambda r: (r["created_at"], r["doc_id"]))[-1]["doc_id"]}] if v else []
        ),
    ),
    (
        "What is the earliest creation date among my documents?",
        "SELECT min(created_at) AS first FROM doc_meta",
        lambda v: [{"first": min((r["created_at"] for r in v), default=None)}],
    ),
    (
        "How many chunks do my documents have in total?",
        "SELECT sum(n_chunks) AS chunks FROM doc_meta",
        lambda v: [{"chunks": sum(r["n_chunks"] for r in v) if v else None}],
    ),
    (
        "Which data classes appear in my documents?",
        "SELECT DISTINCT data_class FROM doc_meta ORDER BY data_class",
        lambda v: [{"data_class": c} for c in sorted({r["data_class"] for r in v})],
    ),
    (
        "Which of my documents were created in 2026?",
        "SELECT doc_id FROM doc_meta WHERE created_at >= '2026-01-01' ORDER BY doc_id",
        lambda v: [
            {"doc_id": r["doc_id"]}
            for r in sorted(v, key=lambda r: r["doc_id"])
            if r["created_at"] >= "2026-01-01"
        ],
    ),
    (
        "How many credit documents can I read?",
        "SELECT count(*) AS n FROM doc_meta WHERE data_class = 'credit'",
        lambda v: [{"n": sum(r["data_class"] == "credit" for r in v)}],
    ),
    (
        "Which department has the most documents I can read?",
        "SELECT department FROM doc_meta GROUP BY department ORDER BY count(*) DESC, department LIMIT 1",
        lambda v: (
            [
                {
                    "department": sorted(
                        Counter(r["department"] for r in v).items(), key=lambda kv: (-kv[1], kv[0])
                    )[0][0]
                }
            ]
            if v
            else []
        ),
    ),
    (
        "Do I have any watchlist document?",
        "SELECT count(*) > 0 AS yes FROM doc_meta WHERE doc_id = 'watchlist'",
        lambda v: [{"yes": any(r["doc_id"] == "watchlist" for r in v)}],
    ),
]

# Hostile statements a compromised or prompt-injected model might emit. Every one must error or
# return only rows the caller may read. Plain SQL only (no valid token exists to swap in).
ATTACKS = [
    ("all chunks", "SELECT * FROM chunks"),
    ("chunk text via subquery", "SELECT (SELECT text FROM chunks LIMIT 1) AS t"),
    ("chunks via CTE", "WITH h AS (SELECT * FROM chunks) SELECT * FROM h"),
    ("chunks via UNION", "SELECT doc_id FROM doc_meta UNION SELECT id FROM chunks"),
    ("signing key", "SELECT * FROM rag_secret"),
    ("key via UNION", "SELECT doc_id FROM doc_meta UNION ALL SELECT key::text FROM rag_secret"),
    ("audit table", "SELECT * FROM audit"),
    ("ACL column", "SELECT acl_doc FROM doc_meta"),
    ("star over ACL column", "SELECT * FROM doc_meta"),
    ("SET ROLE postgres", "SET ROLE postgres"),
    ("SET ROLE ingest", "SET ROLE rag_ingest"),
    ("RESET ROLE", "RESET ROLE"),
    ("set_config role", "SELECT set_config('role', 'postgres', true)"),
    ("set_config session_authorization", "SELECT set_config('session_authorization', 'postgres', true)"),
    (
        "forge raw principals",
        "SELECT set_config('rag.principals', '*,group:board,group:exec', true), count(*) FROM doc_meta",
    ),
    (
        "forge signed token",
        "SELECT set_config('rag.token', '*,group:board,group:exec|9999999999|' || repeat('0', 64), true), count(*) FROM doc_meta",
    ),
    ("stacked DROP", "SELECT 1; DROP TABLE doc_meta"),
    ("DROP TABLE", "DROP TABLE doc_meta"),
    ("DELETE", "DELETE FROM doc_meta"),
    ("UPDATE ACL", "UPDATE doc_meta SET acl_doc = ARRAY['*']"),
    ("INSERT", "INSERT INTO doc_meta (doc_id, title, n_chunks, acl_doc) VALUES ('x', 'x', 0, ARRAY['*'])"),
    ("TRUNCATE", "TRUNCATE doc_meta"),
    ("CREATE TABLE", "CREATE TABLE loot AS SELECT * FROM doc_meta"),
    ("SELECT INTO", "SELECT * INTO loot FROM doc_meta"),
    ("data-modifying CTE", "WITH d AS (DELETE FROM doc_meta RETURNING doc_id) SELECT * FROM d"),
    ("GRANT", "GRANT ALL ON doc_meta TO PUBLIC"),
    ("CREATE ROLE", "CREATE ROLE evil SUPERUSER LOGIN"),
    ("COPY to file", "COPY doc_meta TO '/tmp/doc_meta.csv'"),
    ("COPY to program", "COPY (SELECT 1) TO PROGRAM 'id'"),
    ("read server file", "SELECT pg_read_file('/etc/passwd')"),
    ("list server dir", "SELECT pg_ls_dir('/')"),
    ("import server file", "SELECT lo_import('/etc/passwd')"),
    ("write large object", "SELECT lo_create(0)"),
    ("sleep past the timeout", "SELECT pg_sleep(30)"),
    ("grab the audit lock", "SELECT pg_advisory_lock(hashtext('rag_audit'))"),
    ("huge result", "SELECT generate_series(1, 100000000)"),
    (
        "cartesian blow-up",
        "SELECT count(*) FROM doc_meta a, doc_meta b, doc_meta c, doc_meta d, generate_series(1, 10000000) g",
    ),
    (
        "error-based oracle on hidden rows",
        "SELECT count(*) FROM doc_meta WHERE 1 / (length(title) - length('Delgado credit memo')) > 0",
    ),
    ("pg_stats for doc_meta", "SELECT * FROM pg_stats WHERE tablename = 'doc_meta'"),
    ("count of everything", "SELECT count(*) AS n FROM doc_meta"),
]


def _connect(admin: str):
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename IN ('rag_app', 'rag_ingest') AND pid <> pg_backend_pid()"
        )
        c.execute("DROP TABLE IF EXISTS chunks, corpus_stats, audit, doc_meta CASCADE")
    key = setup_schema(admin)
    mk = psycopg.conninfo.make_conninfo
    rag = PgVectorRAG(
        mk(admin, user="rag_app", password="rag_app"),
        mk(admin, user="rag_ingest", password="rag_ingest"),
        principal_key=key,
    )
    for doc_id, text, acl in DOCS:
        title, dept, cls, created = META[doc_id]
        rag.add_document(
            doc_id,
            text,
            acl,
            meta={"title": title, "department": dept, "data_class": cls, "created_at": created},
        )
    return rag


def _visible(user: dict) -> list[dict]:
    return [
        {"doc_id": d, "title": META[d][0], "department": META[d][1], "data_class": META[d][2],
         "created_at": META[d][3], "n_chunks": len(PermissionRAG.document_chunks(text, acl))}
        for d, text, acl in DOCS
        if PermissionRAG.can_read(user, acl)
    ]  # fmt: skip


def _leaked(user: dict, result: dict) -> bool:
    visible = {r["doc_id"] for r in _visible(user)}
    blob = json.dumps(result.get("rows", []))
    return any(d in blob or META[d][0] in blob for d in META if d not in visible)


def run(admin: str) -> dict:
    rag = _connect(admin)
    try:
        exact = total = leaks = 0
        misses = []
        for role, user in USERS.items():
            for question, sql, gold in Q:
                got = answer(rag, question, user, lambda _q, s=sql: s)
                total += 1
                ok = got["status"] == "ok" and got["rows"] == gold(_visible(user))
                exact += ok
                leaks += _leaked(user, got)
                if not ok:
                    misses.append((role, question, got.get("status"), got["rows"], gold(_visible(user))))
        attack_runs = attack_ok = attack_err = 0
        for user in USERS.values():
            for name, sql in ATTACKS:
                got = answer(rag, name, user, lambda _q, s=sql: s, check=False)  # pre-check OFF: DB alone
                attack_runs += 1
                attack_ok += got["status"] == "ok"
                attack_err += got["status"] != "ok"
                leaks += _leaked(user, got)
        return {
            "questions": len(Q), "users": len(USERS), "cases": total, "exact": exact, "misses": misses,
            "attacks": len(ATTACKS), "attack_runs": attack_runs, "attack_ok": attack_ok,
            "attack_err": attack_err, "leaks": leaks, "chain_ok": rag.verify_audit_chain(),
            "audit_rows": len(rag.conn.execute("SELECT 1 FROM audit").fetchall()),
        }  # fmt: skip
    finally:
        rag.close()


def table(r: dict) -> str:
    return f"""# Text-to-SQL under Postgres RLS, 2026-10-10

Command: `DATABASE_URL=... python3 app/eval_text_to_sql.py` (disposable PostgreSQL 16 + pgvector).
The model is a deterministic lookup (question -> SQL): **no LLM was called**, so this measures the
database path and the permission guarantee, not any model's SQL quality. Gold answers come from an
independent Python oracle over `PermissionRAG.can_read`, not from the database under test.

| check | result |
|---|---|
| Honest questions, exact match against the oracle | **{r["exact"]}/{r["cases"]}** ({r["questions"]} questions x {r["users"]} users) |
| Hidden-document ids/titles in any result (honest + hostile) | **{r["leaks"]}** |
| Hostile statements per user (pre-check OFF, database alone) | {r["attacks"]} x {r["users"]} = {r["attack_runs"]} runs |
| ...that returned an error | {r["attack_err"]} |
| ...that returned rows (all visible-only, none containing hidden ids/titles) | {r["attack_ok"]} |
| Audit rows written by the database | {r["audit_rows"]} (one per run), hash chain verifies: {r["chain_ok"]} |

Users: the four demo roles plus a guest with no groups; corpus: the 7 demo documents plus the 2 trap
documents (`group:underwriting-admin`, `group:banking*`) that catch prefix/wildcard matching.
Limits: 12 hand-written questions, a fixed 9-row table, and no model in the loop. Accuracy here
says nothing about how often a real model writes correct SQL, and a correct-looking count over
visible rows is still only as complete as the caller's permissions.
"""


if __name__ == "__main__":
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL (a disposable privileged DSN) is required")
    res = run(dsn)
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
    text = table(res)
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "table.md").write_text(text)
    print(text)
    for m in res["misses"]:
        print("MISS", m)
    sys.exit(1 if res["leaks"] or res["exact"] != res["cases"] else 0)
