"""Remote Qdrant configuration and bulk permission writes are inspectable."""
from types import SimpleNamespace
from unittest.mock import Mock

from app import config, vectorstore


def test_collection_indexes_levels_and_stores_text_and_original_vectors_on_disk(monkeypatch):
    fake = SimpleNamespace(collection_exists=Mock(return_value=False),
                           create_collection=Mock(), create_payload_index=Mock())
    monkeypatch.setattr(vectorstore, 'client', lambda: fake)
    monkeypatch.setattr(config, 'QDRANT_URL', 'http://localhost:6333')
    vectorstore.reset_collection()
    args = fake.create_collection.call_args.kwargs
    assert args['on_disk_payload'] is True
    assert args['vectors_config'].on_disk is True
    assert args['quantization_config'].scalar.type.value == 'int8'
    assert args['quantization_config'].scalar.always_ram is True
    assert {c.kwargs['field_name'] for c in fake.create_payload_index.call_args_list} == {
        'acl_doc', 'acl_section', 'acl_para', 'doc_id'}
    assert all(c.kwargs['wait'] is True for c in fake.create_payload_index.call_args_list)


def test_one_document_revocation_is_one_filtered_write_without_chunk_reads(monkeypatch):
    fake = SimpleNamespace(set_payload=Mock())
    monkeypatch.setattr(vectorstore, 'client', lambda: fake)
    vectorstore.update_doc_acls({'doc': ['user:owner']})
    fake.set_payload.assert_called_once()
    args=fake.set_payload.call_args.kwargs
    assert args['payload'] == {'acl_doc': ['user:owner']}
    assert args['wait'] is True
    assert len(args['points'].must) == 1
    assert args['points'].must[0].key == 'doc_id'
    assert args['points'].must[0].match.any == ['doc']


def test_bulk_documents_with_same_acl_are_batched_and_different_grants_stay_separate(monkeypatch):
    fake = SimpleNamespace(set_payload=Mock())
    monkeypatch.setattr(vectorstore, 'client', lambda: fake)
    vectorstore.update_doc_acls({'a':['group:hr'],'b':['group:hr'],'c':['group:hr'],'d':[]}, batch_size=2)
    calls=fake.set_payload.call_args_list
    assert len(calls)==3
    mappings={doc:call.kwargs['payload']['acl_doc'] for call in calls
              for doc in call.kwargs['points'].must[0].match.any}
    assert mappings=={'a':['group:hr'],'b':['group:hr'],'c':['group:hr'],'d':[]}


def test_invalid_bulk_configuration_never_writes(monkeypatch):
    import pytest
    fake=SimpleNamespace(set_payload=Mock())
    monkeypatch.setattr(vectorstore, 'client', lambda:fake)
    for kwargs in [{'batch_size':0},{'batch_size':-1},{'batch_size':'x'}]:
        with pytest.raises(ValueError, match='positive integer'):
            vectorstore.update_doc_acls({'doc':['*']}, **kwargs)
    with pytest.raises(ValueError, match='nonempty'):
        vectorstore.update_doc_acls({'valid':['*'], ' ':['*']})
    fake.set_payload.assert_not_called()


def test_bulk_invalid_acl_denies_and_never_rewrites_child_restrictions(monkeypatch):
    fake=SimpleNamespace(set_payload=Mock())
    monkeypatch.setattr(vectorstore,'client',lambda:fake)
    vectorstore.update_doc_acls({'bad':'group:hr'})
    assert fake.set_payload.call_args.kwargs['payload']=={'acl_doc':[]}


def test_sync_replays_one_document_write_for_many_chunks(client,tmp_path,monkeypatch):
    import json
    from app import ingest,sync
    from conftest import reingest
    corpus=tmp_path/'corpus.json'
    corpus.write_text(json.dumps([{'doc_id':'many','acl':['group:hr'],'sections':[
        {'text':'\n\n'.join('salary policy paragraph '+str(i) for i in range(12))}]}]))
    source=tmp_path/'permissions.jsonl'
    source.write_text(json.dumps({'doc_id':'many','acl':['user:owner']}))
    try:
        ingest.ingest_corpus(corpus)
        real=vectorstore.client()
        calls=[]
        original=real.set_payload
        def capture(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)
        monkeypatch.setattr(real,'set_payload',capture)
        assert sync.sync_once(source)==['many']
        assert len(calls)==1
        assert calls[0]['payload']=={'acl_doc':['user:owner']}
    finally:
        reingest()
