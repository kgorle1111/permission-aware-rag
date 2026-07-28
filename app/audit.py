"""Audit trail: who asked, what they asked, what returned, what was hidden.

'denied_count: 3' on a query for "layoff plans" is exactly the insider-risk
signal a security team wants and vanilla RAG never provides.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func

from .identity import Principal
from .store import AuditLog, SessionLocal


def write_audit(principal: Principal, query: str, chunk_ids: list, doc_ids: list,
                denied_count: int, fail_closed: bool):
    with SessionLocal() as s:
        s.add(AuditLog(
            user_id=principal.user_id, groups=list(principal.groups), query=query,
            returned_chunk_ids=[str(c) for c in chunk_ids],
            returned_doc_ids=list(doc_ids),
            denied_count=denied_count, fail_closed=fail_closed,
        ))
        s.commit()


def recent(limit: int = 50) -> list[dict]:
    with SessionLocal() as s:
        rows = s.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
    return [{
        "ts": r.ts.isoformat(), "user": r.user_id, "groups": r.groups,
        "query": r.query, "returned_docs": r.returned_doc_ids,
        "denied_count": r.denied_count, "fail_closed": r.fail_closed,
    } for r in rows]


def denied_heatmap() -> list[dict]:
    """Who is probing for content they can't see — the security-analytics view."""
    with SessionLocal() as s:
        rows = (s.query(AuditLog.user_id, func.count(AuditLog.id), func.sum(AuditLog.denied_count))
                .filter(AuditLog.denied_count > 0)
                .group_by(AuditLog.user_id).all())
    return sorted(
        [{"user": u, "queries_with_denials": q, "total_denied_chunks": int(d or 0)}
         for u, q, d in rows],
        key=lambda r: -r["total_denied_chunks"],
    )
