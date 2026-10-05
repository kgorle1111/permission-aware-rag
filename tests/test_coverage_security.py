"""Security-focused tests for identity, retrieval, ingestion, and sync edges."""
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from conftest import mint, reingest, reset_permission_source
from app import config, identity, ingest, retrieval, sync, main, store
from app.identity import Principal
from app.ingest import strictest
from app.store import ChunkACL, ChunkPolicy, PermissionState, SessionLocal


def test_jwks_verification_uses_cached_client_without_network(monkeypatch):
    class FakeJwksClient:
        def __init__(self):
            self.calls = 0

        def get_signing_key_from_jwt(self, token):
            self.calls += 1
            assert token
            return SimpleNamespace(key=Path(config.JWT_PUBLIC_KEY_PATH).read_text())

    fake_client = FakeJwksClient()
    monkeypatch.setattr(identity.jwt, "PyJWKClient", lambda url: fake_client)
    monkeypatch.setattr(config, "JWKS_URL", "https://idp.invalid/jwks")
    monkeypatch.setattr(identity, "_jwks_client", None)
    principal = identity.verify_token(mint("staff@example.test", ["eng"]))
    assert principal == Principal("staff@example.test", ("eng",))
    assert fake_client.calls == 1


def test_identity_key_and_jwks_failures_return_generic_401(monkeypatch, tmp_path):
    token = mint("staff@example.test", [])

    monkeypatch.setattr(config, "JWKS_URL", "")
    monkeypatch.setattr(config, "JWT_PUBLIC_KEY_PATH", str(tmp_path / "missing.pem"))
    monkeypatch.setattr(identity, "_public_key_cache", None)
    with pytest.raises(HTTPException) as key_error:
        identity.verify_token(token)
    assert key_error.value.status_code == 401
    assert key_error.value.detail == "identity layer unavailable"

    class BrokenJwksClient:
        def get_signing_key_from_jwt(self, _token):
            raise OSError("network detail must not escape")

    monkeypatch.setattr(config, "JWKS_URL", "https://idp.invalid/jwks")
    monkeypatch.setattr(identity, "_jwks_client", BrokenJwksClient())
    with pytest.raises(HTTPException) as jwks_error:
        identity.verify_token(token)
    assert jwks_error.value.status_code == 401
    assert jwks_error.value.detail == "identity layer unavailable"
    assert "network detail" not in jwks_error.value.detail


@pytest.mark.parametrize("groups", ["eng", ["eng", " "], [None]])
def test_malformed_signed_group_claims_are_rejected(groups):
    token = mint("staff@example.test", groups=groups)
    with pytest.raises(HTTPException) as error:
        identity.verify_token(token)
    assert error.value.status_code == 401
    assert "groups claim malformed" in error.value.detail


def test_malformed_signed_subject_is_rejected():
    token = mint("", groups=[])
    with pytest.raises(HTTPException, match="invalid token: groups claim malformed"):
        identity.verify_token(token)


@pytest.mark.parametrize("k", [0, 21, -1])
def test_invalid_result_limits_fail_closed_without_search(client, monkeypatch, k):
    calls = []
    monkeypatch.setattr(retrieval, "search", lambda *a, **kw: calls.append(1))
    response = retrieval.retrieve("salary bands", Principal("bob@company.com", ("hr",)), k=k)
    assert response == retrieval.EMPTY_RESPONSE
    assert calls == []


def test_expired_cache_entry_recomputes_instead_of_serving_stale_result(
        client, monkeypatch):
    search_calls = []
    moments = iter([10.0, 12.0, 13.0])

    class FakeTime:
        @staticmethod
        def monotonic():
            return next(moments)

    def fake_search(_vector, _principals, top_k):
        search_calls.append(top_k)
        return [{"chunk_id": len(search_calls), "doc_id": "handbook",
                 "text": f"fresh-{len(search_calls)}", "score": 0.9}]

    monkeypatch.setattr(retrieval, "time", FakeTime)
    monkeypatch.setattr(config, "CACHE_TTL_S", 1)
    monkeypatch.setattr(retrieval, "embed_one", lambda _query: [0.0])
    monkeypatch.setattr(retrieval, "search", fake_search)
    monkeypatch.setattr(retrieval, "search_unfiltered_count", lambda *_a, **_kw: 0)
    monkeypatch.setattr(retrieval, "answer", lambda _query, rows: rows[0]["text"])
    monkeypatch.setattr(retrieval, "write_audit", lambda *_a, **_kw: None)
    principal = Principal("reader@example.test")

    cold = retrieval.retrieve("cache expiration probe", principal)
    after_expiry = retrieval.retrieve("cache expiration probe", principal)
    assert cold["answer"] == "fresh-1"
    assert after_expiry["answer"] == "fresh-2"
    assert search_calls == [config.TOP_K, config.TOP_K]
    retrieval.clear_cache()


def test_cache_capacity_zero_does_not_retain_responses(client, monkeypatch):
    searches = []

    def fake_search(_vector, _principals, top_k):
        searches.append(top_k)
        return [{"chunk_id": len(searches), "doc_id": "handbook",
                 "text": "vacation policy", "score": 0.9}]

    retrieval.clear_cache()
    monkeypatch.setattr(config, "CACHE_MAX_ENTRIES", 0)
    monkeypatch.setattr(retrieval, "embed_one", lambda _query: [0.0])
    monkeypatch.setattr(retrieval, "search", fake_search)
    monkeypatch.setattr(retrieval, "search_unfiltered_count", lambda *_a, **_kw: 0)
    monkeypatch.setattr(retrieval, "answer", lambda _query, _rows: "answer")
    monkeypatch.setattr(retrieval, "write_audit", lambda *_a, **_kw: None)
    principal = Principal("reader@example.test")

    first = retrieval.retrieve("cache disabled", principal)
    second = retrieval.retrieve("cache disabled", principal)
    assert first == second
    assert searches == [config.TOP_K, config.TOP_K]
    assert not retrieval._cache
    retrieval.clear_cache()


def test_strictest_acl_wildcards_and_empty_opposite_acl():
    assert strictest(["*"], ["*"]) == ["*"]
    assert strictest(["*"], []) == []
    assert strictest([], ["*"]) == []


@pytest.mark.parametrize("corpus", [
    {"doc_id": "not-a-list"},
    [{"doc_id": "", "acl": ["*"], "sections": []}],
    [
        {"doc_id": "duplicate", "acl": ["*"], "sections": []},
        {"doc_id": "duplicate", "acl": ["group:eng"], "sections": []},
    ],
])
def test_ingestion_rejects_invalid_document_shapes_before_mutation(tmp_path, corpus):
    corpus_path = tmp_path / "invalid.json"
    corpus_path.write_text(json.dumps(corpus))
    with pytest.raises(ValueError):
        ingest.ingest_corpus(corpus_path)


def test_empty_full_ingest_clears_stale_vectors_and_acl_rows(client, tmp_path):
    empty_path = tmp_path / "empty-corpus.json"
    empty_path.write_text("[]")
    try:
        assert ingest.ingest_corpus(empty_path) == 0
        with SessionLocal() as session:
            assert session.query(ChunkACL).count() == 0
            assert session.query(ChunkPolicy).count() == 0
            state = session.get(PermissionState, 1)
            assert state.pending is False
            assert state.rebuild_required is False
        response = client.post(
            "/query", json={"query": "salary bands"},
            headers={"Authorization": f"Bearer {mint('bob@company.com', ['hr'])}"})
        assert response.json() == retrieval.EMPTY_RESPONSE
    finally:
        reingest()


def test_incremental_ingestion_is_rejected(client):
    with pytest.raises(ValueError, match="incremental ingestion is unsupported"):
        ingest.ingest_corpus(Path(__file__).resolve().parents[1] / "corpus" / "docs.json",
                             reset=False)


def test_superseded_ingestion_keeps_rebuild_barrier_until_retry(client, monkeypatch):
    real_transaction = ingest.permission_transaction
    transaction_count = 0

    @contextmanager
    def bump_revision_after_barrier():
        nonlocal transaction_count
        with real_transaction() as pair:
            yield pair
        transaction_count += 1
        if transaction_count == 1:
            with real_transaction() as (session, state):
                state.revision += 1

    monkeypatch.setattr(ingest, "permission_transaction", bump_revision_after_barrier)
    corpus_path = Path(__file__).resolve().parents[1] / "corpus" / "docs.json"
    try:
        with pytest.raises(RuntimeError, match="ingestion superseded"):
            ingest.ingest_corpus(corpus_path)
        with SessionLocal() as session:
            state = session.get(PermissionState, 1)
            assert state.pending is True
            assert state.rebuild_required is True
    finally:
        monkeypatch.setattr(ingest, "permission_transaction", real_transaction)
        reingest()


def test_malformed_sync_feed_blocks_reads_until_valid_retry(client):
    try:
        Path(config.PERMISSIONS_SOURCE).write_text('{"doc_id":"hr-salaries",\n')
        with pytest.raises(json.JSONDecodeError):
            sync.sync_once()
        with SessionLocal() as session:
            assert session.get(PermissionState, 1).pending is True

        blocked = client.post(
            "/query", json={"query": "salary bands"},
            headers={"Authorization": f"Bearer {mint('bob@company.com', ['hr'])}"})
        assert blocked.json() == retrieval.EMPTY_RESPONSE

        reset_permission_source()
        sync.sync_once()
        with SessionLocal() as session:
            assert session.get(PermissionState, 1).pending is False
    finally:
        reingest()


def test_missing_policy_row_keeps_sync_pending_until_reingest(client):
    try:
        with SessionLocal() as session:
            first_acl = session.query(ChunkACL).filter_by(doc_id="hr-salaries").first()
            assert first_acl is not None
            session.query(ChunkPolicy).filter_by(chunk_id=first_acl.chunk_id).delete()
            session.commit()

        with pytest.raises(RuntimeError, match="legacy index: full reingestion required"):
            sync.sync_once()
        with SessionLocal() as session:
            assert session.get(PermissionState, 1).pending is True
    finally:
        reingest()


@pytest.mark.parametrize("source", [
    '\n  \n{"doc_id":"", "acl": ["*"]}\n',
    '{"doc_id":"same", "acl": ["*"]}\n{"doc_id":"same", "acl": ["group:hr"]}\n',
    '{"doc_id":"hr-salaries", "deleted":"false", "acl": ["*"]}\n',
])
def test_malformed_permission_rows_fail_closed_until_reingest(client, source):
    try:
        Path(config.PERMISSIONS_SOURCE).write_text(source)
        with pytest.raises(ValueError):
            sync.sync_once()
        with SessionLocal() as session:
            assert session.get(PermissionState, 1).pending is True
    finally:
        reingest()


def test_gdrive_sync_creates_then_updates_checkpoint(client, monkeypatch):
    from app.store import SourceCheckpoint

    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
    monkeypatch.setattr(config, "DRIVE_CREDENTIALS_FILE", "synthetic-creds.json")
    # sync imports these names from the connector module within its GDrive branch.
    from app.connectors import gdrive
    monkeypatch.setattr(gdrive, "build_drive_client", lambda *_args: object())
    responses = iter([({}, "checkpoint-1"), ({}, "checkpoint-2")])
    calls = []

    def fake_load(_drive, saved_token, known_ids):
        calls.append((saved_token, set(known_ids)))
        return next(responses)

    monkeypatch.setattr(gdrive, "load_drive_changes", fake_load)
    try:
        assert sync.sync_once() == []
        with SessionLocal() as session:
            checkpoint = session.get(SourceCheckpoint, "gdrive")
            assert checkpoint.token == "checkpoint-1"
        assert sync.sync_once() == []
        with SessionLocal() as session:
            checkpoint = session.get(SourceCheckpoint, "gdrive")
            assert checkpoint.token == "checkpoint-2"
        assert calls[0][0] is None
        assert calls[1][0] == "checkpoint-1"
        assert calls[0][1]
    finally:
        monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "jsonl")
        reingest()


def test_sync_rejects_missing_gdrive_checkpoint_and_unsupported_backend(client, monkeypatch):
    from app.connectors import gdrive

    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
    monkeypatch.setattr(gdrive, "build_drive_client", lambda *_args: object())
    monkeypatch.setattr(gdrive, "load_drive_changes", lambda *_args: ({}, None))
    try:
        with pytest.raises(ValueError, match="Drive checkpoint missing"):
            sync.sync_once()
        with SessionLocal() as session:
            assert session.get(PermissionState, 1).pending is True

        monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "unsupported")
        with pytest.raises(ValueError, match="unsupported permissions backend"):
            sync.sync_once()
        with SessionLocal() as session:
            assert session.get(PermissionState, 1).pending is True
    finally:
        monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "jsonl")
        reingest()


def test_sync_watch_rejects_zero_interval_and_logs_failed_pass(monkeypatch):
    with pytest.raises(ValueError, match="poll interval must be positive"):
        sync.watch(0)

    calls = []
    class StopWatch(Exception):
        pass

    def fail_sync():
        calls.append("sync")
        raise RuntimeError("transient permission source error")

    def stop_after_first_poll(_interval):
        raise StopWatch

    monkeypatch.setattr(sync, "sync_once", fail_sync)
    monkeypatch.setattr(sync.time, "sleep", stop_after_first_poll)
    with pytest.raises(StopWatch):
        sync.watch(1)
    assert calls == ["sync"]


def test_lifespan_survives_startup_and_poll_failures_and_timeout(monkeypatch, client):
    monkeypatch.setattr(config, "SYNC_INTERVAL_S", 0.005)
    calls = []

    def fail_first_two_passes():
        calls.append("sync")
        if len(calls) <= 2:
            raise RuntimeError("temporary source failure")
        return []

    monkeypatch.setattr(main, "sync_once", fail_first_two_passes)

    async def exercise_lifespan():
        lifespan = main.app.router.lifespan_context(main.app)
        await lifespan.__aenter__()  # first sync failure is caught at startup
        await asyncio.sleep(0.04)  # poll failure, then one or more wait timeouts
        await lifespan.__aexit__(None, None, None)

    asyncio.run(exercise_lifespan())
    assert len(calls) >= 3


def test_store_engine_options_and_missing_permission_state_are_fail_closed(
        monkeypatch, client):
    engine_calls = []
    monkeypatch.setattr(
        store, "create_engine",
        lambda url, **kwargs: engine_calls.append((url, kwargs)) or "fake-engine")
    assert store.make_engine("sqlite:///local.db") == "fake-engine"
    assert engine_calls[-1][1]["connect_args"] == {"check_same_thread": False}
    assert store.make_engine("postgresql://db.example.test/app") == "fake-engine"
    assert "connect_args" not in engine_calls[-1][1]
    assert engine_calls[-1][1]["pool_pre_ping"] is True

    try:
        with SessionLocal() as session:
            session.query(PermissionState).filter_by(id=1).delete()
            session.commit()
        with pytest.raises(RuntimeError, match="permission state missing"):
            with store.permission_transaction():
                pass
    finally:
        store.init_db()
        reingest()


def test_drive_webhook_validates_capability_and_sync_result(monkeypatch, client):
    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "jsonl")
    missing = client.post("/webhooks/drive")
    assert missing.status_code == 404

    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
    monkeypatch.setattr(config, "DRIVE_WEBHOOK_CHANNEL_ID", "channel-123")
    monkeypatch.setattr(config, "DRIVE_WEBHOOK_TOKEN", "secret-token")
    monkeypatch.setattr(config, "DRIVE_WEBHOOK_RESOURCE_ID", "resource-456")
    headers = {
        "x-goog-channel-id": "channel-123",
        "x-goog-channel-token": "secret-token",
        "x-goog-resource-id": "resource-456",
    }

    invalid = client.post("/webhooks/drive", headers={**headers, "x-goog-channel-token": "wrong"})
    assert invalid.status_code == 403

    monkeypatch.setattr(main, "sync_once", lambda: ["changed-doc"])
    assert client.post("/webhooks/drive", headers=headers).status_code == 204

    def failed_sync():
        raise RuntimeError("Drive source unavailable")

    monkeypatch.setattr(main, "sync_once", failed_sync)
    unavailable = client.post("/webhooks/drive", headers=headers)
    assert unavailable.status_code == 503
