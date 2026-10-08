"""Mutation regressions for diagnosable, fail-closed reconciliation inputs."""
import pytest

from app import config, store, sync
from conftest import reingest


@pytest.mark.parametrize('desired, message', [
    ([], 'permission changes must be a document mapping'),
    ({' ': ['*']}, 'invalid document id'),
    ({False: ['*']}, 'invalid document id'),
])
def test_malformed_provider_patch_has_stable_repair_diagnostic_and_no_committed_cursor(
    client, monkeypatch, desired, message
):
    from app.connectors import gdrive

    try:
        with monkeypatch.context() as patch:
            patch.setattr(config, 'PERMISSIONS_BACKEND', 'gdrive')
            patch.setattr(gdrive, 'build_drive_client', lambda *_: object())
            patch.setattr(gdrive, 'load_drive_changes', lambda *_: (desired, 'must-not-commit'))
            with pytest.raises(ValueError) as error:
                sync.sync_once()
            assert str(error.value) == message
            with store.SessionLocal() as session:
                assert session.get(store.PermissionState, 1).pending is True
                assert session.get(store.SourceCheckpoint, 'gdrive') is None
                assert session.get(store.PendingSourceCheckpoint, 'gdrive') is None
                assert session.query(store.PermissionMutation).count() == 0
    finally:
        reingest()


@pytest.mark.parametrize('remove_policy', [False, True])
def test_policy_disappearance_after_validation_cannot_finish_journal(client, tmp_path, monkeypatch, remove_policy):
    import json
    from contextlib import contextmanager

    source = tmp_path / 'patch.jsonl'
    source.write_text(json.dumps({'doc_id': 'hr-salaries', 'acl': []}))
    original = sync.permission_transaction
    calls = 0

    @contextmanager
    def corrupt_between_phases():
        nonlocal calls
        calls += 1
        with original() as (session, state):
            if calls == 3:
                chunk = session.query(store.ChunkACL).filter_by(doc_id='hr-salaries').first()
                policy = session.get(store.ChunkPolicy, chunk.chunk_id)
                if remove_policy:
                    session.delete(policy)
                else:
                    policy.paragraph_acl = None
                session.flush()
            yield session, state

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sync, 'permission_transaction', corrupt_between_phases)
            with pytest.raises(RuntimeError) as error:
                sync.sync_once(source)
            assert str(error.value) == 'legacy index: full reingestion required'
            with store.SessionLocal() as session:
                assert session.get(store.PermissionState, 1).pending is True
                assert session.get(store.PermissionMutation, 'hr-salaries').acl == []
    finally:
        reingest()


# Share the independently owned synthetic SQL/Qdrant fixture, not its assertions.
from test_sync_scale import isolated_index  # noqa: E402,F401


def test_indexed_batches_return_each_affected_chunk_once(isolated_index):
    seed, _, _, _ = isolated_index
    indexed = seed(1001)
    expected_ids = set().union(*indexed.values())
    with store.SessionLocal() as session:
        rows = list(sync._affected_rows(session, indexed))
    actual = [row.chunk_id for row, _ in rows]
    assert set(actual) == expected_ids
    assert len(actual) == len(expected_ids)


def test_jsonl_reader_rejects_whitespace_document_id_before_returning_patch(tmp_path):
    import json
    source = tmp_path / 'blank.jsonl'
    source.write_text(json.dumps({'doc_id': '  ', 'acl': ['*']}))
    with pytest.raises(ValueError) as error:
        sync.read_source(source)
    assert str(error.value) == 'invalid or duplicate document id'


def test_sql_null_child_policy_is_corruption_even_with_an_unrelated_patch(client, tmp_path):
    import json
    from sqlalchemy import null, update

    source = tmp_path / 'other-doc.jsonl'
    source.write_text(json.dumps({'doc_id': 'eng-oncall', 'acl': []}))
    try:
        with store.SessionLocal() as session:
            chunk = session.query(store.ChunkACL).filter_by(doc_id='hr-salaries').first()
            session.execute(update(store.ChunkPolicy).where(store.ChunkPolicy.chunk_id == chunk.chunk_id)
                            .values(paragraph_acl=null()))
            session.commit()
        with pytest.raises(RuntimeError) as error:
            sync.sync_once(source)
        assert str(error.value) == 'legacy index: full reingestion required'
        with store.SessionLocal() as session:
            assert session.get(store.PermissionState, 1).pending is True
    finally:
        reingest()


def test_unchanged_public_child_policy_does_not_create_a_remote_rewrite(client, tmp_path, monkeypatch):
    import json
    from unittest.mock import Mock

    source = tmp_path / 'same.jsonl'
    source.write_text(json.dumps({'doc_id': 'hr-salaries', 'acl': ['group:hr']}))
    try:
        with monkeypatch.context() as patch:
            writes = Mock()
            patch.setattr(sync, 'update_doc_acls', writes)
            assert sync.sync_once(source) == []
            assert not any(call.args[0] for call in writes.call_args_list)
            with store.SessionLocal() as session:
                assert session.query(store.PermissionMutation).count() == 0
    finally:
        reingest()


def test_disagreement_found_after_global_integrity_check_keeps_barrier_and_repair_reason(client, monkeypatch):
    from contextlib import contextmanager

    original = sync.permission_transaction
    current_session = None

    @contextmanager
    def expose_source_transaction():
        nonlocal current_session
        with original() as (session, state):
            current_session = session
            yield session, state

    def altered_source(_):
        chunks = current_session.query(store.ChunkACL).filter_by(doc_id='company-strategy').all()
        assert len(chunks) >= 2
        current_session.get(store.ChunkPolicy, chunks[0].chunk_id).doc_acl = []
        current_session.flush()
        return {'company-strategy': ['*']}

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sync, 'permission_transaction', expose_source_transaction)
            patch.setattr(sync, 'read_source', altered_source)
            with pytest.raises(RuntimeError) as error:
                sync.sync_once()
            assert str(error.value) == 'inconsistent document policy; full reingestion required'
            with store.SessionLocal() as session:
                assert session.get(store.PermissionState, 1).pending is True
                assert session.query(store.PermissionMutation).count() == 0
    finally:
        reingest()


def test_missing_journal_document_explains_full_replacement_requirement(client, tmp_path):
    source = tmp_path / 'empty.jsonl'
    source.write_text('')
    try:
        with store.SessionLocal() as session:
            session.add(store.PermissionMutation(doc_id='not-in-index', acl=[]))
            session.commit()
        with pytest.raises(RuntimeError) as error:
            sync.sync_once(source)
        assert str(error.value) == 'journal document missing; full reingestion required'
        with store.SessionLocal() as session:
            assert session.get(store.PermissionState, 1).pending is True
            assert session.get(store.PermissionMutation, 'not-in-index').acl == []
    finally:
        reingest()


def test_replayed_same_grant_intent_does_not_report_an_acl_change(client, tmp_path, monkeypatch):
    from unittest.mock import Mock
    source = tmp_path / 'empty.jsonl'
    source.write_text('')
    try:
        with store.SessionLocal() as session:
            session.add(store.PermissionMutation(doc_id='hr-salaries', acl=['group:hr']))
            session.commit()
        with monkeypatch.context() as patch:
            writes = Mock()
            patch.setattr(sync, 'update_doc_acls', writes)
            assert sync.sync_once(source) == []
            writes.assert_called_once_with({'hr-salaries': ['group:hr']})
        with store.SessionLocal() as session:
            assert session.query(store.PermissionMutation).count() == 0
            assert session.get(store.PermissionState, 1).pending is False
    finally:
        reingest()


def test_invalid_persisted_cursor_has_stable_repair_diagnostic(client, tmp_path):
    source = tmp_path / 'empty.jsonl'
    source.write_text('')
    try:
        with store.SessionLocal() as session:
            session.add(store.PendingSourceCheckpoint(provider='gdrive', token=''))
            session.commit()
        with pytest.raises(ValueError) as error:
            sync.sync_once(source)
        assert str(error.value) == 'Drive checkpoint missing'
        with store.SessionLocal() as session:
            assert session.get(store.PendingSourceCheckpoint, 'gdrive').token == ''
            assert session.get(store.SourceCheckpoint, 'gdrive') is None
            assert session.get(store.PermissionState, 1).pending is True
    finally:
        reingest()
