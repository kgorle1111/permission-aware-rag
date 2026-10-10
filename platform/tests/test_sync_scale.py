"""Document mutations stay scoped; SQL-only integrity probes retain fail-closed safety."""

import json
import math
from contextlib import contextmanager

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app import config, ingest, store, sync, vectorstore
from app.embeddings import embed_one


@pytest.fixture
def isolated_index(tmp_path, monkeypatch):
    engine = store.make_engine(f"sqlite:///{tmp_path / 'mirror.db'}")
    monkeypatch.setattr(store, "engine", engine)
    monkeypatch.setattr(store, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    monkeypatch.setattr(vectorstore, "_client", client)
    store.init_db()

    def seed(count=80):
        corpus = [{"doc_id": f"d{i:04d}", "acl": ["group:hr"], "sections": [
            {"acl": ["user:bob"], "paragraphs": [
                {"text": f"salary policy ordinary d{i:04d}"},
                {"text": f"salary policy restricted d{i:04d}", "acl": ["group:finance"]},
            ]},
        ]} for i in range(count)]
        path = tmp_path / "corpus.json"
        path.write_text(json.dumps(corpus))
        assert ingest.ingest_corpus(path) == count * 2
        return {f"d{i:04d}": {2*i, 2*i+1} for i in range(count)}

    try:
        yield seed, engine, client, tmp_path
    finally:
        client.close()
        engine.dispose()


def feed(path, desired):
    path.write_text("\n".join(json.dumps({"doc_id": doc_id, "acl": acl})
                              for doc_id, acl in desired.items()) + "\n")


@contextmanager
def watched_reads(engine):
    seen = {"acl": [], "policy": [], "selects": []}

    def acl_loaded(instance, context):
        seen["acl"].append((instance.doc_id, instance.chunk_id))

    def policy_loaded(instance, context):
        seen["policy"].append(instance.chunk_id)

    def executed(conn, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select") and any(f"from {table}" in normalized
                                                     for table in ("chunk_acl", "chunk_policy")):
            seen["selects"].append(normalized)

    event.listen(store.ChunkACL, "load", acl_loaded)
    event.listen(store.ChunkPolicy, "load", policy_loaded)
    event.listen(engine, "before_cursor_execute", executed)
    try:
        yield seen
    finally:
        event.remove(store.ChunkACL, "load", acl_loaded)
        event.remove(store.ChunkPolicy, "load", policy_loaded)
        event.remove(engine, "before_cursor_execute", executed)


def assert_scoped(seen, documents, chunk_ids):
    for statement in seen["selects"]:
        # Bounded, scalar integrity probes may inspect the global mirror. They
        # must not hydrate/replay its entities merely to apply a small patch.
        assert " where " in statement or (" having " in statement and " limit " in statement), statement
    assert {doc for doc, _ in seen["acl"]} <= documents, seen["acl"]
    assert set(seen["policy"]) <= chunk_ids, seen["policy"]
    # Repeated bounded phases are fine; loading the unrelated mirror is not.
    assert len(seen["acl"]) <= 4 * len(chunk_ids)
    assert len(seen["policy"]) <= 4 * len(chunk_ids)


def watch_vector_writes(monkeypatch, client):
    writes = []
    real = client.set_payload

    def counted(**kwargs):
        writes.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(client, "set_payload", counted)
    return writes


def payloads(client, ids):
    return {point.id: point.payload for point in client.retrieve(config.COLLECTION, ids=list(ids),
                                                                with_payload=True, with_vectors=False)}


def test_single_document_sync_reads_only_its_rows_and_preserves_children(isolated_index, monkeypatch):
    seed, engine, client, tmp = isolated_index
    indexed = seed()
    targets = indexed["d0007"]
    before = payloads(client, targets | indexed["d0079"])
    path = tmp / "permissions.jsonl"
    feed(path, {"d0007": ["*"]})
    writes = watch_vector_writes(monkeypatch, client)
    with watched_reads(engine) as seen:
        assert sync.sync_once(path) == ["d0007"]
    assert_scoped(seen, {"d0007"}, targets)
    assert len(writes) == 1
    assert writes[0]["points"].must[0].match.any == ["d0007"]
    after = payloads(client, targets | indexed["d0079"])
    for chunk_id in targets:
        assert after[chunk_id]["acl_doc"] == ["*"]
        assert after[chunk_id]["acl_section"] == before[chunk_id]["acl_section"] == ["user:bob"]
        assert after[chunk_id]["acl_para"] == before[chunk_id]["acl_para"]
    for chunk_id in indexed["d0079"]:
        assert after[chunk_id] == before[chunk_id]
    hits = vectorstore.search(embed_one("salary policy d0007"), ["*", "user:bob"], 20)
    assert {hit["chunk_id"] for hit in hits} == {min(targets)}
    assert vectorstore.search(embed_one("salary policy d0007"), ["*", "user:guest"], 20) == []


def test_same_acl_folder_patch_batches_only_affected_documents(isolated_index, monkeypatch):
    seed, engine, client, tmp = isolated_index
    indexed = seed(1100)
    documents = {f"d{i:04d}" for i in range(1050)}
    targets = set().union(*(indexed[doc] for doc in documents))
    path = tmp / "permissions.jsonl"
    feed(path, dict.fromkeys(sorted(documents), []))
    writes = watch_vector_writes(monkeypatch, client)
    with watched_reads(engine) as seen:
        assert sync.sync_once(path) == sorted(documents)
    assert_scoped(seen, documents, targets)
    assert len(writes) == math.ceil(len(documents) / 1000) == 2
    written = [doc for call in writes for doc in call["points"].must[0].match.any]
    assert len(written) == len(set(written)) == 1050
    assert set(written) == documents
    assert all(call["payload"] == {"acl_doc": []} for call in writes)
    sample = payloads(client, indexed["d0000"] | indexed["d1049"] | indexed["d1099"])
    assert all(sample[i]["acl_doc"] == [] for i in indexed["d0000"] | indexed["d1049"])
    assert all(sample[i]["acl_doc"] == ["group:hr"] for i in indexed["d1099"])


def test_retry_changed_feed_replays_persisted_dirty_intent_after_connection_restart(isolated_index, monkeypatch):
    seed, engine, client, tmp = isolated_index
    indexed = seed()
    path = tmp / "permissions.jsonl"
    feed(path, {"d0007": []})
    real_update = sync.update_doc_acls

    def failed_after_write(acls):
        real_update(acls)
        raise RuntimeError("remote write completed but acknowledgement lost")

    monkeypatch.setattr(sync, "update_doc_acls", failed_after_write)
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        sync.sync_once(path)
    assert all(p["acl_doc"] == [] for p in payloads(client, indexed["d0007"]).values())
    with store.SessionLocal() as session:
        assert session.get(store.PermissionState, 1).pending is True
        old_policies = session.query(store.ChunkPolicy).join(store.ChunkACL,
            store.ChunkACL.chunk_id == store.ChunkPolicy.chunk_id).filter(store.ChunkACL.doc_id == "d0007").all()
        assert len(old_policies) == 2
        assert all(policy.doc_acl == ["group:hr"] for policy in old_policies)

    # A patch omitting A does not cancel its previously committed mutation intent.
    feed(path, {"d0008": []})
    engine.dispose()
    store.init_db()
    monkeypatch.setattr(sync, "update_doc_acls", real_update)
    writes = watch_vector_writes(monkeypatch, client)
    with watched_reads(engine) as seen:
        assert sync.sync_once(path) == ["d0007", "d0008"]
    targets = indexed["d0007"] | indexed["d0008"]
    assert_scoped(seen, {"d0007", "d0008"}, targets)
    assert len(writes) == 1
    assert set(writes[0]["points"].must[0].match.any) == {"d0007", "d0008"}
    assert all(p["acl_doc"] == [] for p in payloads(client, targets).values())
    with store.SessionLocal() as session:
        state = session.get(store.PermissionState, 1)
        assert state.pending is False
        assert state.rebuild_required is False
        assert all(row.acl == [] for row in session.query(store.ChunkACL)
                   .filter(store.ChunkACL.doc_id.in_(["d0007", "d0008"])))
        policies = session.query(store.ChunkPolicy).join(store.ChunkACL,
            store.ChunkACL.chunk_id == store.ChunkPolicy.chunk_id).filter(
                store.ChunkACL.doc_id.in_(["d0007", "d0008"])).all()
        assert len(policies) == 4
        assert all(policy.doc_acl == [] and policy.section_acl == ["user:bob"] for policy in policies)
        assert {tuple(policy.paragraph_acl) for policy in policies} == {("*",), ("group:finance",)}


def test_repeated_unchanged_patch_does_not_rewrite_vectors(isolated_index, monkeypatch):
    seed, engine, client, tmp = isolated_index
    indexed = seed()
    path = tmp / "permissions.jsonl"
    feed(path, {"d0007": ["group:hr"]})
    writes = watch_vector_writes(monkeypatch, client)
    for _ in range(2):
        with watched_reads(engine) as seen:
            assert sync.sync_once(path) == []
        assert_scoped(seen, {"d0007"}, indexed["d0007"])
    assert writes == []
