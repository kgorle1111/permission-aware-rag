"""Regression coverage for permission-sync, cache, and fail-closed edges."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from conftest import auth, mint, reingest

from app import config, retrieval
from app import ingest
from app.identity import Principal
from app.ingest import validate_acl
from app.store import ChunkACL, ChunkPolicy, PermissionState, SessionLocal
from app.sync import sync_once


def _write_source(rows):
    Path(config.PERMISSIONS_SOURCE).write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n")


def test_failed_sync_stays_blocked_and_retry_replays_acl_updates(client, monkeypatch):
    """A partial vector write must leave the durable barrier up until retry."""
    from app import sync
    from app.vectorstore import update_chunk_acl as real_update

    try:
        _write_source([{"doc_id": "hr-salaries", "acl": ["user:cfo@company.com"]}])
        calls = []

        def fail_once(chunk_id, acl):
            calls.append((chunk_id, acl))
            if len(calls) == 1:
                raise RuntimeError("simulated vector write failure")
            return real_update(chunk_id, acl)

        monkeypatch.setattr(sync, "update_chunk_acl", fail_once)
        try:
            sync_once()
            assert False, "the simulated vector failure should escape sync_once"
        except RuntimeError as exc:
            assert "simulated" in str(exc)

        with SessionLocal() as s:
            state = s.get(PermissionState, 1)
            assert state.pending is True
        denied = client.post("/query", json={"query": "salary bands"}, headers=auth("bob"))
        assert denied.json() == retrieval.EMPTY_RESPONSE

        monkeypatch.setattr(sync, "update_chunk_acl", real_update)
        assert sync_once() == ["hr-salaries"]
        allowed_only_to_cfo = client.post(
            "/query", json={"query": "salary bands"}, headers=auth("bob"))
        assert allowed_only_to_cfo.json() == retrieval.EMPTY_RESPONSE
        with SessionLocal() as s:
            state = s.get(PermissionState, 1)
            assert state.pending is False
            assert all(row.acl == ["user:cfo@company.com"]
                       for row in s.query(ChunkACL).filter_by(doc_id="hr-salaries"))
    finally:
        reingest()


def test_failed_full_ingest_requires_reingest_before_sync_can_unblock(client, monkeypatch):
    from app.ingest import upsert_chunks as real_upsert

    def fail_index_write(_chunks):
        raise RuntimeError("simulated index write failure")

    monkeypatch.setattr(ingest, "upsert_chunks", fail_index_write)
    try:
        try:
            ingest.ingest_corpus(ingest.Path(__file__).resolve().parents[1] / "corpus" / "docs.json")
            assert False, "the simulated ingestion failure should escape ingest_corpus"
        except RuntimeError as exc:
            assert "simulated" in str(exc)

        with SessionLocal() as s:
            state = s.get(PermissionState, 1)
            assert state.pending is True
            assert state.rebuild_required is True
        try:
            sync_once()
            assert False, "permission sync cannot repair a failed full index rebuild"
        except RuntimeError as exc:
            assert "reingestion" in str(exc)

        blocked = client.post("/query", json={"query": "vacation policy"}, headers=auth("guest"))
        assert blocked.json() == retrieval.EMPTY_RESPONSE
        with SessionLocal() as s:
            state = s.get(PermissionState, 1)
            assert state.pending is True
            assert state.rebuild_required is True
    finally:
        monkeypatch.setattr(ingest, "upsert_chunks", real_upsert)
        reingest()
    with SessionLocal() as s:
        state = s.get(PermissionState, 1)
        assert state.pending is False
        assert state.rebuild_required is False


def test_section_acl_survives_document_acl_broadening_and_restriction(client):
    """Sync must recompute from the saved section policy, not its old result."""
    try:
        probe = "Financial projections revenue target gross margin FIN-CANARY-4c1d7"
        _write_source([{"doc_id": "company-strategy", "acl": ["*"]}])
        sync_once()
        guest = client.post("/query", json={"query": probe}, headers=auth("guest"))
        finance = client.post("/query", json={"query": probe}, headers=auth("carol"))
        assert "FIN-CANARY-4c1d7" not in guest.text
        assert "FIN-CANARY-4c1d7" in finance.text

        _write_source([{"doc_id": "company-strategy", "acl": ["group:eng"]}])
        sync_once()
        finance_after_restriction = client.post(
            "/query", json={"query": probe}, headers=auth("carol"))
        assert "FIN-CANARY-4c1d7" not in finance_after_restriction.text
        with SessionLocal() as s:
            policies = s.query(ChunkPolicy).join(
                ChunkACL, ChunkPolicy.chunk_id == ChunkACL.chunk_id
            ).filter(ChunkACL.doc_id == "company-strategy").all()
        assert any(policy.section_acl == ["group:finance"] for policy in policies)
    finally:
        reingest()


def test_cache_separates_k_and_unambiguous_principal_scopes(monkeypatch):
    """Neither result limits nor crafted identity delimiters may alias keys."""
    retrieval.clear_cache()
    calls = []

    def fake_search(_vec, principals, top_k):
        calls.append((tuple(principals), top_k))
        return [{"chunk_id": i, "doc_id": f"doc-{i}", "text": f"result-{i}",
                 "score": 0.9} for i in range(1, top_k + 1)]

    monkeypatch.setattr(retrieval, "embed_one", lambda _q: [0.0])
    monkeypatch.setattr(retrieval, "search", fake_search)
    monkeypatch.setattr(retrieval, "search_unfiltered_count", lambda *_a, **_k: 0)
    monkeypatch.setattr(retrieval, "answer", lambda _q, results: str(len(results)))
    monkeypatch.setattr(retrieval, "write_audit", lambda *_a, **_k: None)

    p1 = Principal("x", ("a|group:b",))
    p2 = Principal("x", ("a", "b"))
    assert p1.scope_key != p2.scope_key
    one = retrieval.retrieve("same", p1, k=1)
    two = retrieval.retrieve("same", p1, k=2)
    other_scope = retrieval.retrieve("same", p2, k=1)
    assert len(one["results"]) == 1
    assert len(two["results"]) == 2
    assert len(other_scope["results"]) == 1
    assert calls == [(tuple(p1.principals), 1), (tuple(p1.principals), 2),
                     (tuple(p2.principals), 1)]
    retrieval.clear_cache()


def test_audit_failure_returns_canonical_empty_on_cold_and_warm_cache(client, monkeypatch):
    q = {"query": "salary bands range from 90k to 250k"}
    headers = auth("bob")
    first = client.post("/query", json=q, headers=headers)
    assert first.json()["results"]  # establish a warm cache entry

    def audit_failure(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(retrieval, "write_audit", audit_failure)
    warm_failure = client.post("/query", json=q, headers=headers)
    assert warm_failure.json() == retrieval.EMPTY_RESPONSE

    retrieval.clear_cache()
    cold_failure = client.post("/query", json=q, headers=headers)
    assert cold_failure.json() == retrieval.EMPTY_RESPONSE
    assert cold_failure.content == warm_failure.content


def test_malformed_acl_and_signed_malformed_jwt_claims_fail_closed(client):
    assert validate_acl(None) == []
    assert validate_acl("group:hr") == []
    assert validate_acl(["group:hr", "nobody", "user:", 7, None]) == []
    assert validate_acl(["*", None]) == []

    malformed_groups = mint("guest@external.com", groups="security")
    response = client.post("/query", json={"query": "salary bands"},
                           headers={"Authorization": f"Bearer {malformed_groups}"})
    assert response.status_code == 401


def test_forbidden_and_nonexistent_empty_results_are_byte_identical(client):
    forbidden = client.post("/query", json={"query": "EXEC-CANARY-9d4e2 equity refresh"},
                            headers=auth("guest"))
    missing = client.post("/query", json={"query": "zorblax quantum unicorn farming"},
                          headers=auth("guest"))
    assert forbidden.json()["results"] == missing.json()["results"] == []
    assert forbidden.content == missing.content


def test_delete_and_reingest_invalidate_cached_answers(client):
    try:
        q = {"query": "salary bands range from 90k to 250k"}
        original = client.post("/query", json=q, headers=auth("bob"))
        assert "hr-salaries" in [row["doc_id"] for row in original.json()["results"]]

        _write_source([{"doc_id": "hr-salaries", "deleted": True}])
        assert sync_once() == ["hr-salaries"]
        deleted = client.post("/query", json=q, headers=auth("bob"))
        assert deleted.json() == retrieval.EMPTY_RESPONSE

        reingest()
        restored = client.post("/query", json=q, headers=auth("bob"))
        assert "hr-salaries" in [row["doc_id"] for row in restored.json()["results"]]
    finally:
        reingest()


def test_partial_vector_deletion_cannot_be_restored_by_later_grant(client, monkeypatch):
    """A removed vector point keeps the barrier up even if the source re-grants."""
    from app import sync
    from app.vectorstore import delete_doc as real_delete_doc

    def delete_then_fail(doc_id):
        real_delete_doc(doc_id)
        raise RuntimeError("simulated failure after vector deletion")

    monkeypatch.setattr(sync, "delete_doc", delete_then_fail)
    try:
        _write_source([{"doc_id": "hr-salaries", "deleted": True}])
        try:
            sync_once()
            assert False, "the simulated post-delete failure should escape sync_once"
        except RuntimeError as exc:
            assert "after vector deletion" in str(exc)

        # The vector is gone, but the SQL transaction rolled back. A later
        # source grant cannot recreate the missing vector point through sync.
        monkeypatch.setattr(sync, "delete_doc", real_delete_doc)
        _write_source([{"doc_id": "hr-salaries", "acl": ["group:hr"]}])
        try:
            sync_once()
            assert False, "a source grant must not clear a missing-index barrier"
        except RuntimeError as exc:
            assert "indexed chunk missing" in str(exc)

        blocked = client.post("/query", json={"query": "salary bands"}, headers=auth("bob"))
        assert blocked.json() == retrieval.EMPTY_RESPONSE
        with SessionLocal() as s:
            state = s.get(PermissionState, 1)
            assert state.pending is True
            assert s.query(ChunkACL).filter_by(doc_id="hr-salaries").count() > 0
    finally:
        monkeypatch.setattr(sync, "delete_doc", real_delete_doc)
        reingest()


def test_sync_waits_for_active_retrieval_then_revokes_warmed_answer(client, monkeypatch):
    """A revoke cannot race generation/audit and let the old cached answer survive."""
    from app.identity import Principal

    entered_answer = threading.Event()
    release_answer = threading.Event()
    sync_started = threading.Event()
    original_answer = retrieval.answer
    first_answer = True

    def blocking_answer(query, results):
        nonlocal first_answer
        if first_answer:
            first_answer = False
            entered_answer.set()
            if not release_answer.wait(timeout=8):
                raise TimeoutError("test did not release blocked generation")
        return original_answer(query, results)

    def revoke():
        sync_started.set()
        return sync_once()

    monkeypatch.setattr(retrieval, "answer", blocking_answer)
    retrieval.clear_cache()
    _write_source([{"doc_id": "hr-salaries", "acl": ["user:cfo@company.com"]}])
    pool = ThreadPoolExecutor(max_workers=2)
    query_future = sync_future = None
    try:
        query_future = pool.submit(
            retrieval.retrieve, "salary bands range from 90k to 250k",
            Principal("bob@company.com", ("hr",)))
        assert entered_answer.wait(timeout=5), "retrieval did not reach generation"
        sync_future = pool.submit(revoke)
        assert sync_started.wait(timeout=2), "sync worker did not start"
        time.sleep(0.15)
        assert not sync_future.done(), "sync passed an active retrieval transaction"

        release_answer.set()
        warmed = query_future.result(timeout=5)
        assert any(row["doc_id"] == "hr-salaries" for row in warmed["results"])
        assert sync_future.result(timeout=5) == ["hr-salaries"]

        monkeypatch.setattr(retrieval, "answer", original_answer)
        after_revoke = retrieval.retrieve(
            "salary bands range from 90k to 250k", Principal("bob@company.com", ("hr",)))
        assert after_revoke == retrieval.EMPTY_RESPONSE
    finally:
        release_answer.set()
        if query_future is not None and not query_future.done():
            query_future.cancel()
        if sync_future is not None and not sync_future.done():
            sync_future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        reingest()


def test_failed_audit_commit_never_returns_retrieved_content(client, monkeypatch):
    from sqlalchemy.orm import Session

    original_commit = Session.commit
    failed_once = False

    def fail_first_commit(session):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("simulated audit transaction commit failure")
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_first_commit)
    response = client.post("/query", json={"query": "salary bands range from 90k to 250k"},
                           headers=auth("bob"))
    assert failed_once
    assert response.status_code == 200
    assert response.json() == retrieval.EMPTY_RESPONSE
    assert "hr-salaries" not in response.text
