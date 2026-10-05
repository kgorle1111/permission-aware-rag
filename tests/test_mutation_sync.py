"""Reconciliation contracts exposed by mutation testing."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from app import config, sync
from app.store import ChunkACL, ChunkPolicy, PermissionState, SessionLocal
from conftest import reingest


def test_explicit_source_overrides_default_and_revision_advances_at_each_phase(client, tmp_path, monkeypatch):
    source = tmp_path / "revocation.jsonl"
    source.write_text(json.dumps({"doc_id": "hr-salaries", "acl": []}) + "\n")
    with SessionLocal() as session:
        before = session.get(PermissionState, 1).revision
    original = sync.read_source
    observed = []
    def read(path):
        with SessionLocal() as session:
            state = session.get(PermissionState, 1)
            observed.append(state.revision)
            assert state.pending is True
        return original(path)
    monkeypatch.setattr(sync, "read_source", read)
    try:
        assert sync.sync_once(source) == ["hr-salaries"]
        with SessionLocal() as session:
            state = session.get(PermissionState, 1)
            assert observed[0] > before
            assert state.revision > observed[0]
            assert state.pending is False
            rows = session.query(ChunkACL).filter_by(doc_id="hr-salaries").all()
            assert rows and all(row.acl == [] for row in rows)
            assert all(session.get(ChunkPolicy, row.chunk_id).doc_acl == [] for row in rows)
    finally:
        reingest()


def test_deleting_one_document_does_not_skip_other_rows(client, tmp_path):
    source = tmp_path / "changes.jsonl"
    source.write_text('\n'.join(json.dumps(row) for row in [
        {"doc_id": "handbook", "deleted": True},
        {"doc_id": "hr-salaries", "acl": []},
    ]))
    try:
        assert sync.sync_once(source) == ["handbook", "hr-salaries"]
        with SessionLocal() as session:
            assert session.query(ChunkACL).filter_by(doc_id="handbook").count() == 0
            rows = session.query(ChunkACL).filter_by(doc_id="hr-salaries").all()
            assert rows and all(row.acl == [] for row in rows)
    finally:
        reingest()


def test_doc_policy_changes_are_recorded_even_when_effective_acl_is_unchanged(client, tmp_path):
    # The finance section remains finance-only, but its document policy changes.
    source = tmp_path / "changes.jsonl"
    source.write_text(json.dumps({"doc_id": "company-strategy", "acl": ["group:finance"]}))
    try:
        assert sync.sync_once(source) == ["company-strategy"]
        with SessionLocal() as session:
            rows = session.query(ChunkACL).filter_by(doc_id="company-strategy").all()
            assert rows and all(session.get(ChunkPolicy, r.chunk_id).doc_acl == ["group:finance"] for r in rows)
        assert sync.sync_once(source) == []
    finally:
        reingest()


@pytest.mark.parametrize("token", ["valid-checkpoint", 7, ""])
def test_drive_sync_wires_configured_client_and_rejects_invalid_checkpoint(client, monkeypatch, token):
    from app.connectors import gdrive
    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
    monkeypatch.setattr(config, "DRIVE_CREDENTIALS_FILE", "fixture-credentials.json")
    monkeypatch.setattr(config, "DRIVE_SUBJECT", "delegate@example.test")
    drive = object()
    build = Mock(return_value=drive)
    load = Mock(return_value=({}, token))
    monkeypatch.setattr(gdrive, "build_drive_client", build)
    monkeypatch.setattr(gdrive, "load_drive_changes", load)
    with SessionLocal() as session:
        ids = sorted({r.doc_id for r in session.query(ChunkACL).all()})
    if isinstance(token, str) and token:
        assert sync.sync_once() == []
    else:
        with pytest.raises(ValueError, match="checkpoint"):
            sync.sync_once()
        with SessionLocal() as session:
            assert session.get(PermissionState, 1).pending is True
    build.assert_called_once_with("fixture-credentials.json", "delegate@example.test")
    load.assert_called_once_with(drive, None, ids)


def test_zero_watch_interval_rejects_before_sync_or_sleep(monkeypatch):
    def forbidden(*args):
        pytest.fail("zero poll interval must not perform work")
    monkeypatch.setattr(sync, "sync_once", forbidden)
    monkeypatch.setattr(sync.time, "sleep", forbidden)
    with pytest.raises(ValueError, match="positive"):
        sync.watch(0)


def test_document_policy_change_is_reported_when_all_sections_stay_restricted(client, tmp_path):
    from app.ingest import ingest_corpus
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([{"doc_id": "restricted", "acl": ["*"], "sections": [
        {"acl": ["group:finance"], "text": "section stays restricted"}]}]))
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps({"doc_id": "restricted", "acl": ["group:finance"]}))
    try:
        ingest_corpus(corpus)
        assert sync.sync_once(source) == ["restricted"]
        with SessionLocal() as session:
            row = session.query(ChunkACL).one()
            assert row.acl == ["group:finance"]
            assert session.get(ChunkPolicy, row.chunk_id).doc_acl == ["group:finance"]
        assert sync.sync_once(source) == []
    finally:
        reingest()
