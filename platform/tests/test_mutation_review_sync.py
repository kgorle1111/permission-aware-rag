"""Error-text and revision-arithmetic contracts for app/sync.py."""
import json
import logging
from contextlib import contextmanager

import pytest

from app import config, sync
from app.store import ChunkPolicy, PermissionState, SessionLocal
from conftest import reingest


def _rev():
    with SessionLocal() as s:
        return s.get(PermissionState, 1).revision


@pytest.mark.parametrize("rows,message", [
    ([{"doc_id": "a", "acl": []}, {"doc_id": "a", "acl": []}], "invalid or duplicate document id"),
    ([{"doc_id": "a", "acl": [], "deleted": "yes"}], "deleted must be a boolean"),
])
def test_read_source_error_messages_are_exact(tmp_path, rows, message):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError) as e:
        sync.read_source(p)
    assert str(e.value) == message


def test_sync_advances_revision_by_exactly_one_per_phase(client, tmp_path, monkeypatch):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"doc_id": "hr-salaries", "acl": []}))
    before = _rev()
    seen = []
    orig = sync.read_source
    monkeypatch.setattr(sync, "read_source", lambda path: (seen.append(_rev()), orig(path))[1])
    try:
        sync.sync_once(p)
        assert seen == [before + 1]
        assert _rev() == before + 2
    finally:
        reingest()


def test_superseded_sync_reports_reason(client, monkeypatch):
    real = sync.permission_transaction
    calls = []

    @contextmanager
    def tx():
        with real() as pair:
            yield pair
        calls.append(1)
        if len(calls) == 1:
            with real() as (_, state):
                state.revision += 1

    monkeypatch.setattr(sync, "permission_transaction", tx)
    try:
        with pytest.raises(RuntimeError) as e:
            sync.sync_once()
        assert str(e.value) == "reconciliation superseded by a newer mutation"
    finally:
        monkeypatch.undo()
        reingest()


def test_rebuild_required_reports_reason(client):
    with SessionLocal() as s:
        s.get(PermissionState, 1).rebuild_required = True
        s.commit()
    try:
        with pytest.raises(RuntimeError) as e:
            sync.sync_once()
        assert str(e.value) == "full reingestion required"
    finally:
        with SessionLocal() as s:
            s.get(PermissionState, 1).rebuild_required = False
            s.commit()
        reingest()


def test_unsupported_backend_reports_reason(client, monkeypatch):
    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "bogus")
    try:
        with pytest.raises(ValueError) as e:
            sync.sync_once()
        assert str(e.value) == "unsupported permissions backend"
    finally:
        monkeypatch.undo()
        reingest()


def test_drive_missing_checkpoint_message_is_exact(client, monkeypatch):
    from app.connectors import gdrive
    monkeypatch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
    monkeypatch.setattr(gdrive, "build_drive_client", lambda *a: object())
    monkeypatch.setattr(gdrive, "load_drive_changes", lambda *a: ({}, ""))
    try:
        with pytest.raises(ValueError) as e:
            sync.sync_once()
        assert str(e.value) == "Drive checkpoint missing"
    finally:
        monkeypatch.undo()
        reingest()


def test_legacy_index_reports_reason(client):
    with SessionLocal() as s:
        s.delete(s.query(ChunkPolicy).first())
        s.commit()
    try:
        with pytest.raises(RuntimeError) as e:
            sync.sync_once()
        assert str(e.value) == "legacy index: full reingestion required"
    finally:
        reingest()


def test_watch_logs_exact_failure_message_and_keeps_polling(monkeypatch, caplog):
    class Stop(Exception):
        pass

    def boom():
        raise RuntimeError("x")

    def stop(_):
        raise Stop

    monkeypatch.setattr(sync, "sync_once", boom)
    monkeypatch.setattr(sync.time, "sleep", stop)
    with caplog.at_level(logging.ERROR, logger="permrag"), pytest.raises(Stop):
        sync.watch(1)
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == ["sync failed; queries remain blocked until recovery"]


def test_watch_rejects_with_exact_message():
    with pytest.raises(ValueError) as e:
        sync.watch(-1)
    assert str(e.value) == "poll interval must be positive"
