"""Kills for the shared-lock PR's fail-closed paths: the operator log must say why a query failed closed."""
import logging
from concurrent.futures import ThreadPoolExecutor

from conftest import reingest
from sqlalchemy import delete, update
from test_query_concurrency import BOB, SALARY_Q, _gate_search

from app import retrieval
from app.store import PermissionState, SessionLocal


def _failed_closed_reason(caplog) -> str:
    records = [r for r in caplog.records if r.getMessage() == "retrieval failed closed"]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    return str(records[0].exc_info[1])


# _retrieve 20, 21, 22
def test_missing_permission_state_logs_why_it_failed_closed(client, caplog):
    with SessionLocal() as s:
        row = s.get(PermissionState, 1)
        saved = {c.name: getattr(row, c.name) for c in PermissionState.__table__.columns}
        s.execute(delete(PermissionState))
        s.commit()
    try:
        with caplog.at_level(logging.ERROR, logger="permrag"):
            assert retrieval.retrieve(SALARY_Q, BOB) == retrieval.EMPTY_RESPONSE
        assert _failed_closed_reason(caplog) == "permission state missing; initialize and ingest"
    finally:
        with SessionLocal() as s:  # restore the row so the rest of the suite (and reingest) can run
            s.add(PermissionState(**saved))
            s.commit()
        reingest()


# _retrieve 54, 55, 56
def test_revision_moved_mid_query_logs_why_it_failed_closed(client, monkeypatch, caplog):
    entered, release = _gate_search(monkeypatch, "inflight")
    pool = ThreadPoolExecutor(1, thread_name_prefix="inflight")
    try:
        with caplog.at_level(logging.ERROR, logger="permrag"):
            query = pool.submit(retrieval.retrieve, SALARY_Q, BOB)
            assert entered.wait(5)
            with SessionLocal() as s:
                s.execute(update(PermissionState).where(PermissionState.id == 1)
                          .values(revision=PermissionState.revision + 1))
                s.commit()
            release.set()
            assert query.result(timeout=5) == retrieval.EMPTY_RESPONSE
        assert _failed_closed_reason(caplog) == "permission state changed during retrieval"
    finally:
        release.set()
        pool.shutdown(wait=True)
        reingest()
