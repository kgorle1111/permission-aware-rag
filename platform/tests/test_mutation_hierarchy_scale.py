"""Migration works with PostgreSQL's case-sensitive catalog reflection too."""
from types import SimpleNamespace

from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from app import store


def test_old_policy_migration_uses_exact_catalog_table_name(tmp_path, monkeypatch):
    engine = store.make_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(store, 'engine', engine)
    monkeypatch.setattr(store, 'SessionLocal', sessions)
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE chunk_policy (chunk_id INTEGER PRIMARY KEY, '
                              'doc_acl JSON NOT NULL, section_acl JSON)'))
        actual = inspect(engine)

        def columns(name):
            # PostgreSQL catalogs preserve lowercase names for unquoted tables.
            # SQLite reflection accepts alternate casing and can hide this bug.
            if name != 'chunk_policy':
                raise ValueError('no such table in a case-sensitive catalog')
            return actual.get_columns(name)

        strict = SimpleNamespace(has_table=lambda name: name == 'chunk_policy', get_columns=columns)
        monkeypatch.setattr(store, 'inspect', lambda _: strict)
        store.init_db()
        assert 'paragraph_acl' in {c['name'] for c in inspect(engine).get_columns('chunk_policy')}
        with sessions() as session:
            state = session.get(store.PermissionState, 1)
            assert (state.revision, state.pending, state.rebuild_required) == (0, True, True)
    finally:
        engine.dispose()


def test_remote_indexes_target_collection_and_keyword_grants(monkeypatch):
    from unittest.mock import Mock
    from qdrant_client.models import PayloadSchemaType
    from app import config, vectorstore

    fake = SimpleNamespace(collection_exists=Mock(return_value=False),
                           create_collection=Mock(), create_payload_index=Mock())
    monkeypatch.setattr(vectorstore, 'client', lambda: fake)
    monkeypatch.setattr(config, 'QDRANT_URL', 'http://qdrant:6333')
    vectorstore.reset_collection()
    assert len(fake.create_payload_index.call_args_list) == 4
    for call in fake.create_payload_index.call_args_list:
        assert call.kwargs['collection_name'] == config.COLLECTION
        assert call.kwargs['field_schema'] == PayloadSchemaType.KEYWORD


def test_default_bulk_write_limits_documents_per_request_and_accepts_one(monkeypatch):
    from unittest.mock import Mock
    from app import vectorstore

    fake = SimpleNamespace(set_payload=Mock())
    monkeypatch.setattr(vectorstore, 'client', lambda: fake)
    documents = {f'doc-{i}': ['group:hr'] for i in range(1001)}
    vectorstore.update_doc_acls(documents)
    batches = [call.kwargs['points'].must[0].match.any
               for call in fake.set_payload.call_args_list]
    assert [len(batch) for batch in batches] == [1000, 1]
    assert set(sum(batches, [])) == set(documents)
    fake.set_payload.reset_mock()
    vectorstore.update_doc_acls({'a': ['*'], 'b': ['*']}, batch_size=1)
    assert [call.kwargs['points'].must[0].match.any
            for call in fake.set_payload.call_args_list] == [['a'], ['b']]


def test_invalid_bulk_input_gives_specific_error_without_writes(monkeypatch):
    import pytest
    from unittest.mock import Mock
    from app import vectorstore

    fake = SimpleNamespace(set_payload=Mock())
    monkeypatch.setattr(vectorstore, 'client', lambda: fake)
    with pytest.raises(ValueError) as error:
        vectorstore.update_doc_acls({'a': ['*']}, batch_size=0)
    assert str(error.value) == 'batch_size must be a positive integer'
    with pytest.raises(ValueError) as error:
        vectorstore.update_doc_acls({'a': ['*'], '': ['*']})
    assert str(error.value) == 'document ids must be nonempty'
    fake.set_payload.assert_not_called()


def test_ingestion_rejects_ambiguous_sections_and_explains_expected_shape():
    import pytest
    from app.ingest import chunk_document

    for section in [{'text': 'flat', 'paragraphs': [{'text': 'nested'}]},
                    {'paragraphs': 'not a list'}]:
        with pytest.raises(ValueError) as error:
            chunk_document({'doc_id': 'ambiguous', 'acl': ['*'], 'sections': [section]})
        assert str(error.value) == 'section requires either text or a paragraphs list'


def test_bad_paragraph_text_explains_type_and_blank_does_not_drop_later_content():
    import pytest
    from app.ingest import chunk_document

    with pytest.raises(ValueError) as error:
        chunk_document({'doc_id': 'bad-text', 'acl': ['*'], 'sections': [
            {'paragraphs': [{'text': None}]}]})
    assert str(error.value) == 'paragraph text must be a string'
    chunks = chunk_document({'doc_id': 'blanks', 'acl': ['*'], 'sections': [
        {'paragraphs': [{'text': 'first'}, {'text': '  '}, {'text': 'last', 'acl': ['group:hr']}]}]})
    assert [chunk['text'] for chunk in chunks] == ['first', 'last']
    assert chunks[1]['acl_para'] == ['group:hr']


def test_superseded_bulk_sync_reports_retry_reason_and_keeps_read_barrier(client, monkeypatch):
    from contextlib import contextmanager
    from unittest.mock import Mock
    import pytest
    from app import sync
    from conftest import reingest

    original = sync.permission_transaction
    calls = 0

    @contextmanager
    def newer_mutation():
        nonlocal calls
        calls += 1
        with original() as (session, state):
            if calls == 3:
                state.revision += 1
            yield session, state

    try:
        monkeypatch.setattr(sync, 'permission_transaction', newer_mutation)
        writes = Mock()
        monkeypatch.setattr(sync, 'update_doc_acls', writes)
        with pytest.raises(RuntimeError) as error:
            sync.sync_once()
        assert str(error.value) == 'reconciliation superseded by a newer mutation'
        writes.assert_not_called()
        with store.SessionLocal() as session:
            assert session.get(store.PermissionState, 1).pending is True
    finally:
        reingest()


def test_inconsistent_document_grants_require_reingestion_and_do_not_write_vectors(client, tmp_path, monkeypatch):
    import json
    import pytest
    from unittest.mock import Mock
    from app import ingest, sync
    from conftest import reingest

    corpus = tmp_path / 'inconsistent.json'
    corpus.write_text(json.dumps([{'doc_id': 'multi', 'acl': ['group:hr'],
                                  'sections': [{'text': 'first\n\nsecond'}]}]))
    try:
        ingest.ingest_corpus(corpus)
        with store.SessionLocal() as session:
            session.get(store.ChunkPolicy, 1).doc_acl = ['group:eng']
            session.commit()
        monkeypatch.setattr(sync, 'read_source', lambda _: {})
        writes = Mock()
        monkeypatch.setattr(sync, 'update_doc_acls', writes)
        with pytest.raises(RuntimeError) as error:
            sync.sync_once()
        assert str(error.value) == 'inconsistent document policy; full reingestion required'
        writes.assert_not_called()
        with store.SessionLocal() as session:
            assert session.get(store.PermissionState, 1).pending is True
    finally:
        reingest()
