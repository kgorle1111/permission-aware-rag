"""Adversarial retry cases for durable document reconciliation intents."""
import json

import pytest
from sqlalchemy.orm import Session

from app import config, ingest, retrieval, sync, vectorstore
from app.store import ChunkACL, ChunkPolicy, PermissionState, SessionLocal, SourceCheckpoint
from conftest import CORPUS, auth, reingest


def _source(tmp_path, rows):
    path = tmp_path / "patch.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    return path


def _blocked(client):
    assert client.get("/readyz").status_code == 503
    response = client.post("/query", json={"query": "salary bands"}, headers=auth("bob"))
    assert response.json() == retrieval.EMPTY_RESPONSE
    with SessionLocal() as session:
        assert session.get(PermissionState, 1).pending is True


def _doc_policies(doc_id):
    with SessionLocal() as session:
        rows = session.query(ChunkPolicy).join(
            ChunkACL, ChunkACL.chunk_id == ChunkPolicy.chunk_id
        ).filter(ChunkACL.doc_id == doc_id).all()
        assert rows
        return [list(acl) for acl in sorted({tuple(row.doc_acl) for row in rows})]


def _vector_grants(doc_id):
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    points, _ = vectorstore.client().scroll(
        collection_name=config.COLLECTION,
        scroll_filter=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
        with_payload=True, limit=1000)
    assert points, "the test document must remain indexed"
    return [list(acl) for acl in sorted({tuple(point.payload["acl_doc"]) for point in points})]


@pytest.mark.parametrize("failure", ["remote", "sql_commit"])
def test_retry_with_different_patch_keeps_prior_durable_revoke(client, tmp_path, monkeypatch, failure):
    """A new patch omitting a failed revoke cannot silently resurrect its grants."""
    source = _source(tmp_path, [{"doc_id": "hr-salaries", "acl": []}])
    real_update = vectorstore.update_doc_acls
    real_commit = Session.commit
    remote_completed = False

    def write_then_fail(acls):
        nonlocal remote_completed
        real_update(acls)
        remote_completed = True
        if failure == "remote":
            raise RuntimeError("failure after remote success")

    def commit_failure(session):
        if remote_completed:
            raise RuntimeError("failure after remote before SQL commit")
        return real_commit(session)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sync, "update_doc_acls", write_then_fail)
            if failure == "sql_commit":
                patch.setattr(Session, "commit", commit_failure)
            with pytest.raises(RuntimeError, match="after remote"):
                sync.sync_once(source)
        assert _vector_grants("hr-salaries") == [[]]
        assert _doc_policies("hr-salaries") == [["group:hr"]]
        _blocked(client)
        source = _source(tmp_path, [{"doc_id": "eng-oncall", "acl": ["user:alice@company.com"]}])
        changed = sync.sync_once(source)
        assert set(changed) == {"hr-salaries", "eng-oncall"}
        assert _doc_policies("hr-salaries") == [[]]
        assert _vector_grants("hr-salaries") == [[]]
        assert _doc_policies("eng-oncall") == [["user:alice@company.com"]]
        assert _vector_grants("eng-oncall") == [["user:alice@company.com"]]
        assert client.get("/readyz").status_code == 200
        assert client.post("/query", json={"query": "salary bands"},
                           headers=auth("bob")).json() == retrieval.EMPTY_RESPONSE
    finally:
        reingest()


def test_explicit_new_same_document_patch_supersedes_failed_grant(client, tmp_path, monkeypatch):
    """Latest explicit intent wins, while section/paragraph restrictions survive."""
    source = _source(tmp_path, [{"doc_id": "company-strategy", "acl": ["*"]}])
    real_update = vectorstore.update_doc_acls

    def grant_then_fail(acls):
        real_update(acls)
        raise RuntimeError("partial grant")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sync, "update_doc_acls", grant_then_fail)
            with pytest.raises(RuntimeError, match="partial grant"):
                sync.sync_once(source)
        _blocked(client)
        source = _source(tmp_path, [{"doc_id": "company-strategy", "acl": []}])
        assert sync.sync_once(source) == ["company-strategy"]
        assert all(acl == [] for acl in _doc_policies("company-strategy"))
        assert all(acl == [] for acl in _vector_grants("company-strategy"))
        with SessionLocal() as session:
            policies = session.query(ChunkPolicy).join(
                ChunkACL, ChunkACL.chunk_id == ChunkPolicy.chunk_id
            ).filter(ChunkACL.doc_id == "company-strategy").all()
            assert any(policy.section_acl == ["group:finance"] for policy in policies)
            assert all(policy.paragraph_acl == ["*"] for policy in policies)
    finally:
        reingest()


@pytest.mark.parametrize("bad_rows", [
    [{"doc_id": "hr-salaries", "acl": []}, {"doc_id": "hr-salaries", "acl": ["*"]}],
    [{"doc_id": "hr-salaries", "acl": []}, {"doc_id": "eng-oncall", "deleted": "false"}],
])
def test_malformed_followup_never_discards_saved_revocation(client, tmp_path, monkeypatch, bad_rows):
    source = _source(tmp_path, [{"doc_id": "hr-salaries", "acl": []}])
    real_update = vectorstore.update_doc_acls

    def fail_after_revoke(acls):
        real_update(acls)
        raise RuntimeError("partial revoke")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sync, "update_doc_acls", fail_after_revoke)
            with pytest.raises(RuntimeError, match="partial revoke"):
                sync.sync_once(source)
        with pytest.raises(ValueError):
            sync.sync_once(_source(tmp_path, bad_rows))
        _blocked(client)
        sync.sync_once(_source(tmp_path, []))
        assert _doc_policies("hr-salaries") == [[]]
        assert _vector_grants("hr-salaries") == [[]]
    finally:
        reingest()


def test_full_replacement_discards_stale_document_and_drive_intents(client, tmp_path, monkeypatch):
    """A completed full rebuild starts from its corpus, never old failed intents."""
    from app.connectors import gdrive

    real_update = vectorstore.update_doc_acls

    def fail_after_revoke(acls):
        real_update(acls)
        raise RuntimeError("partial Drive revoke")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
            patch.setattr(gdrive, "build_drive_client", lambda *_args: object())
            patch.setattr(gdrive, "load_drive_changes", lambda *_args: ({"hr-salaries": []}, "stale-token"))
            patch.setattr(sync, "update_doc_acls", fail_after_revoke)
            with pytest.raises(RuntimeError, match="partial Drive revoke"):
                sync.sync_once()
        _blocked(client)
        ingest.ingest_corpus(CORPUS)
        sync.sync_once(_source(tmp_path, []))
        assert _doc_policies("hr-salaries") == [["group:hr"]]
        assert _vector_grants("hr-salaries") == [["group:hr"]]
        with SessionLocal() as session:
            assert session.get(SourceCheckpoint, "gdrive") is None
            assert session.get(PermissionState, 1).pending is False
    finally:
        reingest()


def test_drive_checkpoint_advances_only_with_all_saved_and_latest_intents(client, monkeypatch):
    """The next Drive delta cannot drop a prior revoke or advance on SQL failure."""
    from app.connectors import gdrive

    saved_tokens = []
    outcomes = [({"hr-salaries": []}, "token-two"),
                ({"eng-oncall": []}, "token-three")]
    real_update = vectorstore.update_doc_acls
    real_commit = Session.commit
    remote_completed = False

    def load(_drive, saved_token, _ids):
        saved_tokens.append(saved_token)
        return outcomes[len(saved_tokens) - 1]

    def write(acls):
        nonlocal remote_completed
        real_update(acls)
        remote_completed = True

    def fail_final_commit(session):
        if remote_completed:
            raise RuntimeError("Drive SQL commit failed")
        return real_commit(session)

    try:
        with SessionLocal() as session:
            session.add(SourceCheckpoint(provider="gdrive", token="token-one"))
            session.commit()
        with monkeypatch.context() as patch:
            patch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
            patch.setattr(gdrive, "build_drive_client", lambda *_args: object())
            patch.setattr(gdrive, "load_drive_changes", load)
            with monkeypatch.context() as failure_patch:
                failure_patch.setattr(sync, "update_doc_acls", write)
                failure_patch.setattr(Session, "commit", fail_final_commit)
                with pytest.raises(RuntimeError, match="Drive SQL commit failed"):
                    sync.sync_once()
            with SessionLocal() as session:
                assert session.get(SourceCheckpoint, "gdrive").token == "token-one"
            _blocked(client)
            assert set(sync.sync_once()) == {"hr-salaries", "eng-oncall"}
        assert saved_tokens == ["token-one", "token-one"]
        assert _vector_grants("hr-salaries") == [[]]
        assert _vector_grants("eng-oncall") == [[]]
        assert _doc_policies("hr-salaries") == [[]]
        with SessionLocal() as session:
            assert session.get(SourceCheckpoint, "gdrive").token == "token-three"
            assert session.get(PermissionState, 1).pending is False
    finally:
        reingest()


def test_superseded_mutation_preserves_journal_for_next_retry(client, tmp_path, monkeypatch):
    """Revision changes between journal persistence and application stay blocked."""
    from contextlib import contextmanager

    original_transaction = sync.permission_transaction
    calls = 0

    @contextmanager
    def supersede_after_intent():
        nonlocal calls
        calls += 1
        current_call = calls
        with original_transaction() as transaction:
            yield transaction
        if current_call == 2:
            with original_transaction() as (_session, state):
                state.revision += 1
                state.pending = True

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sync, "permission_transaction", supersede_after_intent)
            with pytest.raises(RuntimeError, match="superseded"):
                sync.sync_once(_source(tmp_path, [{"doc_id": "hr-salaries", "acl": []}]))
        _blocked(client)
        assert sync.sync_once(_source(tmp_path, [])) == ["hr-salaries"]
        assert _doc_policies("hr-salaries") == [[]]
        assert _vector_grants("hr-salaries") == [[]]
    finally:
        reingest()


def test_deleted_vectors_cannot_be_regranted_and_full_rebuild_cleans_intents(client, tmp_path, monkeypatch):
    """Only successful replacement may unblock after a partial remote deletion."""
    real_delete = vectorstore.delete_doc
    real_upsert = ingest.upsert_chunks

    def delete_then_fail(doc_id):
        real_delete(doc_id)
        raise RuntimeError("deleted before failure")

    def fail_rebuild(_chunks):
        raise RuntimeError("replacement unavailable")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sync, "delete_doc", delete_then_fail)
            with pytest.raises(RuntimeError, match="deleted before failure"):
                sync.sync_once(_source(tmp_path, [{"doc_id": "hr-salaries", "deleted": True}]))
        _blocked(client)
        with pytest.raises(RuntimeError, match="full reingestion"):
            sync.sync_once(_source(tmp_path, [{"doc_id": "hr-salaries", "acl": ["*"]}]))
        with monkeypatch.context() as patch:
            patch.setattr(ingest, "upsert_chunks", fail_rebuild)
            with pytest.raises(RuntimeError, match="replacement unavailable"):
                ingest.ingest_corpus(CORPUS)
        _blocked(client)
        with pytest.raises(RuntimeError, match="full reingestion"):
            sync.sync_once(_source(tmp_path, []))
        monkeypatch.setattr(ingest, "upsert_chunks", real_upsert)
        ingest.ingest_corpus(CORPUS)
        sync.sync_once(_source(tmp_path, []))
        assert _doc_policies("hr-salaries") == [["group:hr"]]
        assert _vector_grants("hr-salaries") == [["group:hr"]]
        assert client.get("/readyz").status_code == 200
    finally:
        reingest()


@pytest.mark.parametrize("bad_id", ["", " ", 12])
def test_invalid_drive_document_id_never_advances_checkpoint(client, monkeypatch, bad_id):
    from app.connectors import gdrive

    try:
        with monkeypatch.context() as patch:
            patch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
            patch.setattr(gdrive, "build_drive_client", lambda *_args: object())
            patch.setattr(gdrive, "load_drive_changes", lambda *_args: ({bad_id: ["*"]}, "bad-token"))
            with pytest.raises(ValueError):
                sync.sync_once()
        _blocked(client)
        with SessionLocal() as session:
            assert session.get(SourceCheckpoint, "gdrive") is None
        assert _doc_policies("hr-salaries") == [["group:hr"]]
    finally:
        reingest()


def test_malformed_drive_acl_is_denial_in_both_sql_and_vector_store(client, monkeypatch):
    from app.connectors import gdrive

    try:
        with monkeypatch.context() as patch:
            patch.setattr(config, "PERMISSIONS_BACKEND", "gdrive")
            patch.setattr(gdrive, "build_drive_client", lambda *_args: object())
            patch.setattr(gdrive, "load_drive_changes", lambda *_args: (
                {"hr-salaries": ["*", "unqualified-invalid"]}, "deny-token"))
            assert sync.sync_once() == ["hr-salaries"]
        assert _doc_policies("hr-salaries") == [[]]
        assert _vector_grants("hr-salaries") == [[]]
        assert client.post("/query", json={"query": "salary bands"},
                           headers=auth("bob")).json() == retrieval.EMPTY_RESPONSE
    finally:
        reingest()


def test_corrupt_saved_acl_cannot_replay_wildcard_from_mixed_invalid_list(client, tmp_path):
    from app.store import PermissionMutation

    try:
        with SessionLocal() as session:
            session.add(PermissionMutation(doc_id="hr-salaries", acl=["*", "invalid-grant"]))
            session.get(PermissionState, 1).pending = True
            session.commit()
        sync.sync_once(_source(tmp_path, []))
        assert _doc_policies("hr-salaries") == [[]]
        assert _vector_grants("hr-salaries") == [[]]
    finally:
        reingest()


def test_corrupt_saved_checkpoint_never_clears_barrier(client, tmp_path):
    from app.store import PendingSourceCheckpoint

    try:
        with SessionLocal() as session:
            session.add(PendingSourceCheckpoint(provider="gdrive", token=""))
            session.get(PermissionState, 1).pending = True
            session.commit()
        with pytest.raises(ValueError, match="checkpoint"):
            sync.sync_once(_source(tmp_path, []))
        _blocked(client)
        with SessionLocal() as session:
            assert session.get(SourceCheckpoint, "gdrive") is None
            assert session.get(PendingSourceCheckpoint, "gdrive").token == ""
    finally:
        reingest()


@pytest.mark.parametrize("patch_rows", [[], [{"doc_id": "eng-oncall", "acl": []}]])
def test_unrelated_json_null_paragraph_policy_stays_fail_closed(client, tmp_path, patch_rows):
    """SQL JSON null must be caught even outside the indexed affected documents."""
    try:
        with SessionLocal() as session:
            policy = session.query(ChunkPolicy).join(
                ChunkACL, ChunkACL.chunk_id == ChunkPolicy.chunk_id
            ).filter(ChunkACL.doc_id == "hr-salaries").first()
            policy.paragraph_acl = None
            session.commit()
        with pytest.raises(RuntimeError, match="reingestion"):
            sync.sync_once(_source(tmp_path, patch_rows))
        _blocked(client)
    finally:
        reingest()


def test_unknown_pending_checkpoint_provider_stays_fail_closed(client, tmp_path):
    from app.store import PendingSourceCheckpoint

    try:
        with SessionLocal() as session:
            session.add(PendingSourceCheckpoint(provider="unknown-provider", token="token"))
            session.get(PermissionState, 1).pending = True
            session.commit()
        with pytest.raises(ValueError, match="checkpoint"):
            sync.sync_once(_source(tmp_path, []))
        _blocked(client)
        with SessionLocal() as session:
            assert session.get(SourceCheckpoint, "unknown-provider") is None
            assert session.get(PendingSourceCheckpoint, "unknown-provider").token == "token"
    finally:
        reingest()


def test_saved_intent_for_missing_document_requires_full_reingestion(client, tmp_path):
    from app.store import PermissionMutation

    try:
        with SessionLocal() as session:
            session.add(PermissionMutation(doc_id="missing-mirror-document", acl=["*"]))
            session.get(PermissionState, 1).pending = True
            session.commit()
        with pytest.raises(RuntimeError, match="journal document missing"):
            sync.sync_once(_source(tmp_path, []))
        _blocked(client)
        with SessionLocal() as session:
            assert session.get(PermissionMutation, "missing-mirror-document").acl == ["*"]
    finally:
        reingest()
