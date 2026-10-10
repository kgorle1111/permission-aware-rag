"""Mocked Drive reconciliation and webhook integration contracts."""

import pytest

from app import config
from app.store import ChunkACL, PermissionState, SessionLocal, SourceCheckpoint
from app.sync import sync_once


def _install_drive(monkeypatch, loader):
    from app.connectors import gdrive

    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
    monkeypatch.setattr(gdrive, "build_drive_client", lambda *_args: object())
    monkeypatch.setattr(gdrive, "load_drive_changes", loader)


def _checkpoint(token):
    with SessionLocal() as session:
        row = session.get(SourceCheckpoint, "gdrive")
        if row is None:
            session.add(SourceCheckpoint(provider="gdrive", token=token))
        else:
            row.token = token
        session.commit()


def _reset_corpus():
    from conftest import CORPUS
    from app.ingest import ingest_corpus

    ingest_corpus(CORPUS)


def test_drive_revoke_and_checkpoint_commit_with_effective_acl(client, monkeypatch):
    from conftest import CORPUS
    from app.ingest import ingest_corpus

    _reset_corpus()
    seen = []

    def load(_drive, saved_token, known_ids):
        seen.append((saved_token, set(known_ids)))
        return {"hr-salaries": []}, "drive-token-2"

    _install_drive(monkeypatch, load)
    _checkpoint("drive-token-1")

    try:
        changed = sync_once()

        assert "hr-salaries" in changed
        assert seen[0][0] == "drive-token-1"
        assert "hr-salaries" in seen[0][1]
        with SessionLocal() as session:
            row = session.query(ChunkACL).filter_by(doc_id="hr-salaries").first()
            checkpoint = session.get(SourceCheckpoint, "gdrive")
            state = session.get(PermissionState, 1)
            assert row.acl == []
            assert checkpoint.token == "drive-token-2"
            assert state.pending is False
    finally:
        ingest_corpus(CORPUS)


def test_drive_vector_failure_rolls_back_checkpoint_and_retry_uses_old_token(client, monkeypatch):
    from conftest import CORPUS
    from app import sync as sync_module
    from app.ingest import ingest_corpus
    from app.vectorstore import update_doc_acls as real_update

    _reset_corpus()
    saved_tokens = []

    def load(_drive, saved_token, _known_ids):
        saved_tokens.append(saved_token)
        return {"hr-salaries": []}, "drive-token-2"

    _install_drive(monkeypatch, load)
    _checkpoint("drive-token-1")

    def fail_vector_write(*_args, **_kwargs):
        raise RuntimeError("vector store unavailable")

    monkeypatch.setattr(sync_module, "update_doc_acls", fail_vector_write)
    try:
        with pytest.raises(RuntimeError, match="vector store unavailable"):
            sync_once()

        with SessionLocal() as session:
            checkpoint = session.get(SourceCheckpoint, "gdrive")
            state = session.get(PermissionState, 1)
            row = session.query(ChunkACL).filter_by(doc_id="hr-salaries").first()
            assert checkpoint.token == "drive-token-1"
            assert state.pending is True
            assert row.acl == ["group:hr"]

        monkeypatch.setattr(sync_module, "update_doc_acls", real_update)
        assert "hr-salaries" in sync_once()
        assert saved_tokens == ["drive-token-1", "drive-token-1"]
        with SessionLocal() as session:
            checkpoint = session.get(SourceCheckpoint, "gdrive")
            state = session.get(PermissionState, 1)
            assert checkpoint.token == "drive-token-2"
            assert state.pending is False
    finally:
        ingest_corpus(CORPUS)


def test_full_reingestion_clears_drive_checkpoint(client, monkeypatch):
    from app.ingest import ingest_corpus
    from conftest import CORPUS

    _reset_corpus()
    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
    _checkpoint("stale-drive-token")

    assert ingest_corpus(CORPUS) > 0

    with SessionLocal() as session:
        assert session.get(SourceCheckpoint, "gdrive") is None


def test_drive_webhook_validates_channel_and_maps_sync_outcomes(client, monkeypatch):
    from app import main

    _reset_corpus()
    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
    monkeypatch.setattr(config, "DRIVE_WEBHOOK_CHANNEL_ID", "channel-123")
    monkeypatch.setattr(config, "DRIVE_WEBHOOK_TOKEN", "secret-token")
    monkeypatch.setattr(config, "DRIVE_WEBHOOK_RESOURCE_ID", "resource-456")
    headers = {
        "X-Goog-Channel-Id": "channel-123",
        "X-Goog-Channel-Token": "secret-token",
        "X-Goog-Resource-Id": "resource-456",
    }
    calls = []
    monkeypatch.setattr(main, "sync_once", lambda: calls.append("sync"))

    accepted = client.post("/webhooks/drive", headers=headers)
    assert accepted.status_code == 204
    assert accepted.content == b""
    assert calls == ["sync"]

    invalid = dict(headers, **{"X-Goog-Channel-Token": "wrong-token"})
    rejected = client.post("/webhooks/drive", headers=invalid)
    assert rejected.status_code == 403
    assert calls == ["sync"]

    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "jsonl")
    unconfigured = client.post("/webhooks/drive", headers=headers)
    assert unconfigured.status_code == 404
    assert calls == ["sync"]

    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")

    def fail_sync():
        raise RuntimeError("Drive unavailable")

    monkeypatch.setattr(main, "sync_once", fail_sync)
    failed = client.post("/webhooks/drive", headers=headers)
    assert failed.status_code == 503


def test_readyz_returns_service_unavailable_while_permissions_pending(client):
    _reset_corpus()
    with SessionLocal() as session:
        state = session.get(PermissionState, 1)
        previous_pending = state.pending
        previous_rebuild = state.rebuild_required
        state.pending = True
        state.rebuild_required = False
        session.commit()

    try:
        assert client.get("/readyz").status_code == 503
    finally:
        with SessionLocal() as session:
            state = session.get(PermissionState, 1)
            state.pending = previous_pending
            state.rebuild_required = previous_rebuild
            session.commit()
