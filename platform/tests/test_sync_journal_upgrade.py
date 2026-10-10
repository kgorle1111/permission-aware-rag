"""A pre-journal lost acknowledgement must remain blocked across an upgrade."""
import json

import pytest
from sqlalchemy import inspect

from app import config, ingest, retrieval, store, sync, vectorstore
from app.identity import Principal
from conftest import CORPUS, reingest


def _drop_journal_tables(missing='both'):
    if missing in ('both', 'checkpoint'):
        store.PendingSourceCheckpoint.__table__.drop(store.engine)
    if missing in ('both', 'documents'):
        store.PermissionMutation.__table__.drop(store.engine)


def _old_remote_write_then_rollback():
    # The prior release committed the barrier, then wrote remote payloads
    # inside a SQL transaction without persisting document-specific intents.
    with store.permission_transaction() as (_session, state):
        state.pending = True
        state.revision += 1
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        with store.permission_transaction():
            vectorstore.update_doc_acls({'hr-salaries': ['*']})
            raise RuntimeError('lost acknowledgement after remote success')


@pytest.mark.parametrize('missing', ['both', 'documents', 'checkpoint'])
def test_pending_prejournal_upgrade_cannot_clear_unknown_remote_grant(client, tmp_path, missing):
    source = tmp_path / 'empty.jsonl'
    source.write_text('')
    try:
        _drop_journal_tables(missing)
        _old_remote_write_then_rollback()
        with store.SessionLocal() as session:
            state = session.get(store.PermissionState, 1)
            old_revision = state.revision
            assert state.pending is True
            assert state.rebuild_required is False
            assert all(policy.doc_acl == ['group:hr'] for policy in session.query(store.ChunkPolicy)
                       .join(store.ChunkACL, store.ChunkACL.chunk_id == store.ChunkPolicy.chunk_id)
                       .filter(store.ChunkACL.doc_id == 'hr-salaries'))
            chunk_ids = [row.chunk_id for row in session.query(store.ChunkACL)
                         .filter_by(doc_id='hr-salaries')]
        points = vectorstore.client().retrieve(config.COLLECTION, ids=chunk_ids, with_payload=True)
        assert len(points) == len(chunk_ids) > 0
        assert all(point.payload['acl_doc'] == ['*'] for point in points)
        store.init_db()
        with store.SessionLocal() as session:
            state = session.get(store.PermissionState, 1)
            assert (state.revision, state.pending, state.rebuild_required) == (old_revision + 1, True, True)
        with pytest.raises(RuntimeError, match='full reingestion required'):
            sync.sync_once(source)
        assert retrieval.retrieve('salary bands HR-CANARY-2b8c1', Principal('guest', ())) == retrieval.EMPTY_RESPONSE
        with store.SessionLocal() as session:
            state = session.get(store.PermissionState, 1)
            assert state.pending is True
            assert state.rebuild_required is True
            assert state.revision > old_revision
        store.init_db()
        with store.SessionLocal() as session:
            assert session.get(store.PermissionState, 1).rebuild_required is True
        ingest.ingest_corpus(CORPUS)
        sync.sync_once(source)
        assert retrieval.retrieve('salary bands HR-CANARY-2b8c1', Principal('guest', ())) == retrieval.EMPTY_RESPONSE
        with store.SessionLocal() as session:
            assert session.get(store.PermissionState, 1).pending is False
    finally:
        store.init_db()
        reingest()


def test_clean_prejournal_index_upgrade_preserves_ready_mirror(client, tmp_path):
    source = tmp_path / 'empty.jsonl'
    source.write_text('')
    try:
        _drop_journal_tables()
        with store.SessionLocal() as session:
            state = session.get(store.PermissionState, 1)
            old_revision = state.revision
            assert state.pending is False
        store.init_db()
        with store.SessionLocal() as session:
            state = session.get(store.PermissionState, 1)
            assert (state.revision, state.pending, state.rebuild_required) == (old_revision, False, False)
        assert inspect(store.engine).has_table('permission_mutation')
        assert inspect(store.engine).has_table('pending_source_checkpoint')
        assert sync.sync_once(source) == []
    finally:
        store.init_db()
        reingest()


def test_current_journal_retry_does_not_require_unnecessary_full_reingestion(client, tmp_path, monkeypatch):
    source = tmp_path / 'patch.jsonl'
    source.write_text(json.dumps({'doc_id': 'hr-salaries', 'acl': []}))
    real_update = vectorstore.update_doc_acls

    def lost_ack(acls):
        real_update(acls)
        raise RuntimeError('lost acknowledgement')

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sync, 'update_doc_acls', lost_ack)
            with pytest.raises(RuntimeError, match='lost acknowledgement'):
                sync.sync_once(source)
        store.init_db()
        with store.SessionLocal() as session:
            assert session.get(store.PermissionMutation, 'hr-salaries').acl == []
            assert session.get(store.PermissionState, 1).rebuild_required is False
        source.write_text('')
        assert sync.sync_once(source) == ['hr-salaries']
        with store.SessionLocal() as session:
            assert session.get(store.PermissionState, 1).pending is False
            assert session.query(store.PermissionMutation).count() == 0
    finally:
        reingest()


def test_upgrade_barrier_is_committed_before_schema_bootstrap_can_crash(client, monkeypatch):
    original_create = store.Base.metadata.create_all

    def create_then_crash(engine):
        original_create(engine)
        raise RuntimeError('crash after creating journal tables')

    try:
        _drop_journal_tables()
        _old_remote_write_then_rollback()
        with monkeypatch.context() as patch:
            patch.setattr(store.Base.metadata, 'create_all', create_then_crash)
            with pytest.raises(RuntimeError, match='crash after creating'):
                store.init_db()
        assert inspect(store.engine).has_table('permission_mutation')
        assert inspect(store.engine).has_table('pending_source_checkpoint')
        with store.SessionLocal() as session:
            state = session.get(store.PermissionState, 1)
            assert state.pending is True
            assert state.rebuild_required is True
            revision = state.revision
        store.init_db()
        with store.SessionLocal() as session:
            state = session.get(store.PermissionState, 1)
            assert (state.revision, state.pending, state.rebuild_required) == (revision, True, True)
    finally:
        store.init_db()
        reingest()


def test_upgrade_does_not_replace_an_already_committed_rebuild_intent(client):
    try:
        _drop_journal_tables()
        with store.SessionLocal() as session:
            state = session.get(store.PermissionState, 1)
            state.pending = True
            state.rebuild_required = True
            session.commit()
            revision = state.revision
        store.init_db()
        with store.SessionLocal() as session:
            state = session.get(store.PermissionState, 1)
            assert (state.revision, state.pending, state.rebuild_required) == (revision, True, True)
    finally:
        store.init_db()
        reingest()


def test_journal_upgrade_updates_only_the_active_permission_state_row(client):
    # Bootstrap mutations must use the same active row as runtime locking;
    # an unrelated stored row must not acquire a new mutation intent.
    try:
        _drop_journal_tables()
        with store.SessionLocal() as session:
            session.add(store.PermissionState(id=2, revision=987, pending=True, rebuild_required=False))
            state = session.get(store.PermissionState, 1)
            state.pending = True
            session.commit()
        store.init_db()
        with store.SessionLocal() as session:
            active = session.get(store.PermissionState, 1)
            other = session.get(store.PermissionState, 2)
            assert active.rebuild_required is True
            assert (other.revision, other.pending, other.rebuild_required) == (987, True, False)
    finally:
        with store.SessionLocal() as session:
            session.query(store.PermissionState).filter_by(id=2).delete()
            session.commit()
        store.init_db()
        reingest()
