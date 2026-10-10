"""Query, cache and audit contracts verified against persisted state."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import audit, config, retrieval, store
from app.identity import Principal


def test_retrieval_preserves_query_vector_result_schema_and_success_audit(client, monkeypatch):
    vector = [0.125, 0.5]
    principal = Principal("reader", ("hr",))
    monkeypatch.setattr(retrieval, "embed_one", Mock(return_value=vector))
    search = Mock(return_value=[{"chunk_id": 7, "doc_id": "permitted", "text": "content", "score": 0.123456}])
    denied = Mock(return_value=2)
    answer = Mock(return_value="answer")
    monkeypatch.setattr(retrieval, "search", search)
    monkeypatch.setattr(retrieval, "search_unfiltered_count", denied)
    monkeypatch.setattr(retrieval, "answer", answer)
    result = retrieval.retrieve("original question", principal, k=20)
    assert result == {"results": [{"doc_id": "permitted", "text": "content", "score": 0.1235}], "answer": "answer",
                      "degraded": False, "source": "full_rag"}
    search.assert_called_once_with(vector, principal.principals, top_k=20)
    denied.assert_called_once_with(vector, principal.principals, top_k=20)
    answer.assert_called_once_with("original question", result["results"])
    with store.SessionLocal() as session:
        record = session.query(store.AuditLog).one()
        assert record.user_id == "reader" and record.groups == ["hr"]
        assert record.query == "[redacted]"
        assert record.returned_chunk_ids == ["7"] and record.returned_doc_ids == ["permitted"]
        assert record.denied_count == 2 and record.fail_closed is False


def test_failed_retrieval_redacts_query_without_fabricated_denials(client, monkeypatch):
    with store.SessionLocal() as session:   # permission uncertainty, not a dependency outage
        session.query(store.PermissionState).update({"pending": True})
        session.commit()
    try:
        assert retrieval.retrieve("original failed question", Principal("reader")) == retrieval.EMPTY_RESPONSE
    finally:
        with store.SessionLocal() as session:
            session.query(store.PermissionState).update({"pending": False})
            session.commit()
    with store.SessionLocal() as session:
        record = session.query(store.AuditLog).one()
        assert record.query == "[redacted]"
        assert record.returned_chunk_ids == [] and record.returned_doc_ids == []
        assert record.denied_count == 0 and record.fail_closed is True


def test_cache_ttl_boundary_and_expiration_require_fresh_search(client, monkeypatch):
    current = [100.0]
    monkeypatch.setattr(retrieval, "time", SimpleNamespace(monotonic=lambda: current[0]))
    monkeypatch.setattr(config, "CACHE_TTL_S", 5.0)
    search = Mock(return_value=[])
    monkeypatch.setattr(retrieval, "search", search)
    principal = Principal("reader")
    retrieval.retrieve("cache boundary", principal)
    assert search.call_count == 1
    current[0] = 105.0
    retrieval.retrieve("cache boundary", principal)
    assert search.call_count == 1
    current[0] = 105.001
    retrieval.retrieve("cache boundary", principal)
    assert search.call_count == 2


def test_new_database_starts_at_pending_revision_zero(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    engine = store.make_engine(f"sqlite:///{tmp_path / 'new.db'}")
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(store, "engine", engine)
    monkeypatch.setattr(store, "SessionLocal", sessions)
    try:
        store.init_db()
        with sessions() as session:
            state = session.get(store.PermissionState, 1)
            assert state.revision == 0 and state.pending is True
            assert state.rebuild_required is True
    finally:
        engine.dispose()


def test_init_db_creates_canonical_permission_row_even_with_other_ids(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    engine = store.make_engine(f"sqlite:///{tmp_path / 'existing.db'}")
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(store, "engine", engine)
    monkeypatch.setattr(store, "SessionLocal", sessions)
    try:
        store.Base.metadata.create_all(engine)
        with sessions() as session:
            session.add(store.PermissionState(id=2, revision=7, pending=True))
            session.commit()
        store.init_db()
        with sessions() as session:
            state = session.get(store.PermissionState, 1)
            assert state is not None and state.pending is True
            assert session.get(store.PermissionState, 2).revision == 7
    finally:
        engine.dispose()
