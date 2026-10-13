"""Text-to-SQL over document metadata, where Postgres, not a SQL parser, enforces who sees what (T25).

An LLM turns a question into one SELECT over `doc_meta`. The statement runs in the database as the
`rag_sql` role (pgvector_rag.rag_sql_run): it can read only the non-ACL columns of doc_meta, rows
are filtered by the same signed-principal RLS as chunks, and the audit row is written by the
database. This module's pre-check is defence in depth, never the guarantee; the adversarial tests
call the database path with the pre-check bypassed.

The model is an injected callable (question -> SQL text). Tests and evals use deterministic fakes.
"""

import re
from collections.abc import Callable

SCHEMA_PROMPT = """Table doc_meta(doc_id text, title text, department text, data_class text, created_at date, n_chunks integer).
One row per document the caller may read. Answer with ONE PostgreSQL SELECT over doc_meta and nothing else."""

MAX_SQL_LEN = 2000
_START = re.compile(r"(select|with)\b", re.IGNORECASE)


class SqlRejected(ValueError):
    """The pre-check refused the statement; it was audited and never sent for execution."""


def precheck(sql: str) -> str:
    """One SELECT/WITH statement, no comments, no ';' inside. Returns it without a trailing ';'.

    Deliberately blunt (a ';' inside a string literal is refused too): the database is the guarantee.
    """
    if not isinstance(sql, str):
        raise SqlRejected("model output is not text")
    stmt = sql.strip().removesuffix(";").strip()
    if not stmt or len(stmt) > MAX_SQL_LEN:
        raise SqlRejected("empty or oversized statement")
    if ";" in stmt or "--" in stmt or "/*" in stmt:
        raise SqlRejected("multiple statements or comments")
    if not _START.match(stmt):
        raise SqlRejected("only SELECT statements")
    return stmt


def answer(rag, question: str, user: dict, llm: Callable[[str], str], *, check: bool = True) -> dict:
    """question -> {"sql", "status", "rows", "truncated"[, "sqlstate"]}; every attempt is audited.

    `check=False` skips the pre-check so tests can prove the database alone holds the line.
    """
    sql = llm(question)
    if check:
        try:
            sql = precheck(sql)
        except SqlRejected:
            rag.run_sql(question, sql if isinstance(sql, str) else "", user, rejected=True)
            return {"sql": sql, "status": "rejected", "rows": [], "truncated": False}
    res = rag.run_sql(question, sql, user)
    return {"sql": sql, "rows": [], "truncated": False, **res}


def anthropic_llm(question: str) -> str:
    """Optional real model (needs ANTHROPIC_API_KEY): schema + question in, SQL text out."""
    import os

    import llm

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    body = {
        "model": llm.MODEL,
        "max_tokens": 300,
        "system": SCHEMA_PROMPT,
        "messages": [{"role": "user", "content": question}],
    }
    return llm._post(body, key, 30)["content"][0]["text"]
