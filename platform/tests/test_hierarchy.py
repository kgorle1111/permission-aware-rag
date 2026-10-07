"""The engine and sync enforce independent ACL levels before ranking."""
import json

from app import ingest, sync, vectorstore
from app.embeddings import embed_one
from conftest import reingest


def test_engine_all_levels_and_paragraph_boundary(client, tmp_path):
    source = tmp_path / 'nested.json'
    source.write_text(json.dumps([{'doc_id': 'nested', 'acl': ['group:hr'], 'sections': [
        {'acl': ['user:bob'], 'paragraphs': [
            {'text': 'salary policy ordinary details'},
            {'text': 'salary policy secret compensation', 'acl': ['group:executive']},
        ]},
    ]}]))
    try:
        ingest.ingest_corpus(source)
        q = embed_one('salary policy')
        hits = vectorstore.search(q, ['*', 'user:bob', 'group:hr'], 20)
        assert [h['text'] for h in hits] == ['salary policy ordinary details']
        assert vectorstore.search(q, ['*', 'user:bob'], 20) == []
        assert vectorstore.search(q, ['*', 'user:alice', 'group:hr'], 20) == []
        assert len(vectorstore.search(q, ['*', 'user:bob', 'group:hr', 'group:executive'], 20)) == 2
        permissions = tmp_path / 'permissions.jsonl'
        permissions.write_text(json.dumps({'doc_id':'nested','acl':['*']}))
        sync.sync_once(permissions)
        assert len(vectorstore.search(q, ['*', 'user:bob'], 20)) == 1
        assert vectorstore.search(q, ['*', 'user:alice'], 20) == []
    finally:
        reingest()


def test_missing_acl_levels_deny_legacy_points(client):
    from qdrant_client.models import PointStruct
    from app import config
    vectorstore.client().upsert(config.COLLECTION, wait=True, points=[PointStruct(
        id=9999, vector=embed_one('legacy public secret'),
        payload={'doc_id':'legacy','text':'legacy public secret','acl':['*']})])
    try:
        assert all(h['doc_id'] != 'legacy' for h in vectorstore.search(embed_one('legacy public secret'),['*'],20))
    finally:
        reingest()
