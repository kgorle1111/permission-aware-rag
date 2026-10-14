"""Queries must not serialize on the permission write lock, and must still never outlive a revocation.

The old design held the SQLite/Postgres row lock for the whole request (embed, search, LLM, audit),
so ~97% of server time at 25 VUs was queueing. Now a query reads the permission state without a
write lock, does its work, and a short closing transaction re-checks that state before the audit
row is written: if a mutation barrier landed in between, the query fails closed.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import update
from conftest import reingest

from app import config, retrieval
from app.identity import Principal
from app.store import AuditLog, PermissionState, SessionLocal
from app.sync import sync_once

BOB = Principal("bob@company.com", ("hr",))
SALARY_Q = "salary bands range from 90k to 250k"


def _gate_search(monkeypatch, gated_query_marker: str):
    """Let `search` run for real, then park the calling thread until released.

    Only the thread whose first search call is for the gated query parks; the rest pass through.
    """
    entered, release = threading.Event(), threading.Event()
    real = retrieval.search
    parked = {"done": False}

    def search(qvec, principals, top_k, **kw):
        hits = real(qvec, principals, top_k=top_k, **kw)
        if threading.current_thread().name.startswith(gated_query_marker) and not parked["done"]:
            parked["done"] = True
            entered.set()
            assert release.wait(10), "test never released the parked query"
        return hits

    monkeypatch.setattr(retrieval, "search", search)
    return entered, release


def test_queries_run_concurrently_while_one_is_in_flight(client, monkeypatch):
    """A parked query must not stop another from finishing (it held the write lock before)."""
    entered, release = _gate_search(monkeypatch, "parked")
    parked = ThreadPoolExecutor(1, thread_name_prefix="parked")
    other = ThreadPoolExecutor(1, thread_name_prefix="other")
    try:
        slow = parked.submit(retrieval.retrieve, "vacation policy", Principal("g@x.com"))
        assert entered.wait(5), "first query never reached the vector search"
        fast = other.submit(retrieval.retrieve, "expense reports", Principal("g@x.com"))
        done = fast.result(timeout=3)  # before the fix this blocks on the held lock
        assert done["source"] == "full_rag"
        with SessionLocal() as s:
            assert s.query(AuditLog).filter_by(fail_closed=True).count() == 0
        assert not slow.done()
    finally:
        release.set()
        parked.shutdown(wait=True)
        other.shutdown(wait=True)
    assert slow.result(timeout=5)["source"] == "full_rag"


def test_revocation_committing_mid_query_never_returns_revoked_content(client, monkeypatch):
    """The invariant: a revocation that commits between the permission check and the result wins.

    The query has already retrieved the HR chunk when the revoke runs to completion on another
    thread; the closing re-check sees the moved revision and the query fails closed.
    """
    entered, release = _gate_search(monkeypatch, "inflight")
    pool = ThreadPoolExecutor(1, thread_name_prefix="inflight")
    try:
        query = pool.submit(retrieval.retrieve, SALARY_Q, BOB)
        assert entered.wait(5), "query never reached the vector search"
        Path(config.PERMISSIONS_SOURCE).write_text(
            json.dumps({"doc_id": "hr-salaries", "acl": ["user:cfo@company.com"]}) + "\n")
        assert sync_once() == ["hr-salaries"]  # not blocked by the in-flight query
        release.set()
        result = query.result(timeout=5)
    finally:
        release.set()
        pool.shutdown(wait=True)
        reingest()
    assert result == retrieval.EMPTY_RESPONSE
    assert "HR-CANARY" not in json.dumps(result)


def test_revoked_midflight_query_is_audited_failed_closed_and_never_cached(client, monkeypatch):
    entered, release = _gate_search(monkeypatch, "inflight")
    pool = ThreadPoolExecutor(1, thread_name_prefix="inflight")
    try:
        query = pool.submit(retrieval.retrieve, SALARY_Q, BOB)
        assert entered.wait(5)
        Path(config.PERMISSIONS_SOURCE).write_text(
            json.dumps({"doc_id": "hr-salaries", "acl": ["user:cfo@company.com"]}) + "\n")
        sync_once()
        release.set()
        assert query.result(timeout=5) == retrieval.EMPTY_RESPONSE
        with SessionLocal() as s:
            last = s.query(AuditLog).order_by(AuditLog.id.desc()).first()
        assert last.fail_closed is True and last.returned_doc_ids == []
        # Same query again under the new revision: revoked, so empty, and nothing stale cached.
        assert retrieval.retrieve(SALARY_Q, BOB) == retrieval.EMPTY_RESPONSE
    finally:
        release.set()
        pool.shutdown(wait=True)
        reingest()


@pytest.mark.parametrize("change", [{"revision": PermissionState.revision + 1},
                                    {"pending": True}, {"rebuild_required": True}])
def test_any_permission_state_change_mid_query_fails_it_closed(client, monkeypatch, change):
    """Each field the closing re-check reads is independently sufficient to abort the query."""
    entered, release = _gate_search(monkeypatch, "inflight")
    pool = ThreadPoolExecutor(1, thread_name_prefix="inflight")
    try:
        query = pool.submit(retrieval.retrieve, SALARY_Q, BOB)
        assert entered.wait(5)
        with SessionLocal() as s:
            s.execute(update(PermissionState).where(PermissionState.id == 1).values(**change))
            s.commit()
        release.set()
        assert query.result(timeout=5) == retrieval.EMPTY_RESPONSE
    finally:
        release.set()
        pool.shutdown(wait=True)
        reingest()
