"""Audit metadata records identities, returned IDs and denial counts.

Query text is never persisted in new records. Legacy query columns are masked
at presentation without modifying historical rows.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func

from .identity import Principal
from .store import AuditLog, SessionLocal


def write_audit(principal: Principal, query: str, chunk_ids: list, doc_ids: list,
                denied_count: int, fail_closed: bool, session=None):
    def add(s):
        s.add(AuditLog(
            user_id=principal.user_id, groups=list(principal.groups), query="[redacted]",
            returned_chunk_ids=[str(c) for c in chunk_ids],
            returned_doc_ids=list(doc_ids),
            denied_count=denied_count, fail_closed=fail_closed,
        ))
        s.flush()
    if session is not None:
        add(session)
    else:
        with SessionLocal() as s:
            add(s)
            s.commit()


def recent(limit: int = 50) -> list[dict]:
    with SessionLocal() as s:
        rows = s.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
    return [{
        "ts": r.ts.isoformat(), "user": r.user_id, "groups": r.groups,
        "query": "[redacted]", "returned_docs": r.returned_doc_ids,
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
