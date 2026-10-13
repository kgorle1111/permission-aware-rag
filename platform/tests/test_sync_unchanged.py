"""A poll that finds nothing to change must not raise the permission barrier.

Raising it (pending + revision bump) fails every in-flight and newly arriving query closed, so a
10 s poll that changed nothing still cost a burst of fail-closed responses. A real change must
still raise the barrier before anything is applied.
"""
import json
from pathlib import Path

from conftest import reingest

from app import config, retrieval
from app.identity import Principal
from app.store import PermissionState, SessionLocal
from app.sync import sync_once

BOB = Principal("bob@company.com", ("hr",))
Q = "salary bands range from 90k to 250k"


def _state():
    with SessionLocal() as s:
        st = s.get(PermissionState, 1)
        return st.revision, st.pending, st.rebuild_required


def _write(rows):
    Path(config.PERMISSIONS_SOURCE).write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _bob_sees_hr():
    retrieval.clear_cache()
    return any(r["doc_id"] == "hr-salaries" for r in retrieval.retrieve(Q, BOB)["results"])


def test_unchanged_poll_leaves_revision_and_barrier_untouched(client):
    before = _state()
    assert before[1:] == (False, False)
    assert sync_once() == []
    assert sync_once() == []
    assert _state() == before
    assert _bob_sees_hr()


def test_change_still_raises_barrier_applies_and_revokes(client):
    before = _state()
    _write([{"doc_id": "hr-salaries", "acl": ["user:cfo@company.com"]}])
    try:
        assert sync_once() == ["hr-salaries"]
        after = _state()
        assert after[0] >= before[0] + 2 and after[1:] == (False, False)  # barrier + completion
        assert not _bob_sees_hr()
    finally:
        reingest()


def test_changed_then_reverted_source_still_applies_both_ways(client):
    original = Path(config.PERMISSIONS_SOURCE).read_text()
    try:
        _write([{"doc_id": "hr-salaries", "acl": ["user:cfo@company.com"]}])
        sync_once()
        assert not _bob_sees_hr()
        revoked_state = _state()
        Path(config.PERMISSIONS_SOURCE).write_text(original)
        assert sync_once() == ["hr-salaries"]
        assert _state()[0] > revoked_state[0]
        assert _bob_sees_hr()
    finally:
        reingest()


def test_pending_barrier_is_never_skipped(client):
    """An unchanged source must not clear or bypass a standing barrier."""
    with SessionLocal() as s:
        s.get(PermissionState, 1).pending = True
        s.commit()
    try:
        before = _state()
        sync_once()
        assert _state()[0] > before[0]  # went through the full path
    finally:
        reingest()


def test_malformed_source_still_raises_barrier(client):
    Path(config.PERMISSIONS_SOURCE).write_text('{"doc_id":"hr-salaries",\n')
    try:
        try:
            sync_once()
            assert False, "malformed source must fail"
        except json.JSONDecodeError:
            pass
        assert _state()[1] is True
    finally:
        reingest()


def test_unchanged_poll_does_not_abort_an_in_flight_query(client, monkeypatch):
    """With lock-free queries (perf/permission-shared-lock) an unchanged poll must not fail them."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered, release, real = threading.Event(), threading.Event(), retrieval.search

    def parked(qvec, principals, top_k, **kw):
        hits = real(qvec, principals, top_k=top_k, **kw)
        entered.set()
        assert release.wait(10)
        return hits

    monkeypatch.setattr(retrieval, "search", parked)
    with ThreadPoolExecutor(1) as pool:
        query = pool.submit(retrieval.retrieve, Q, BOB)
        assert entered.wait(5)
        assert sync_once() == []  # the 10 s poll lands mid-query and finds nothing
        release.set()
        result = query.result(timeout=5)
    assert any(r["doc_id"] == "hr-salaries" for r in result["results"])
