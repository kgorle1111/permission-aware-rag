"""Independent regressions authored after the reviewer's fixes.

Expectations derive from AGENTS.md's no-query-text logging invariant and
fail-closed permission state. Runtime source and existing tests were not read.
"""

import json

import pytest

from conftest import mint, reingest
from app.identity import Principal
from app.ingest import ingest_corpus
from app.retrieval import retrieve
from app.store import AuditLog, ChunkACL, PermissionState, SessionLocal


@pytest.fixture
def audit_corpus(client, tmp_path):
    corpus = [
        {"doc_id": "public-audit-control", "acl": ["*"],
         "sections": [{"text": "cobalt memorandum public evidence"}]},
        {"doc_id": "restricted-audit-control", "acl": ["group:restricted"],
         "sections": [{"text": "cobalt memorandum private evidence"}]},
    ]
    path = tmp_path / "independent-audit-corpus.json"
    path.write_text(json.dumps(corpus))
    assert ingest_corpus(path) == 2
    yield client
    reingest()


def headers(user, groups):
    return {"Authorization": f"Bearer {mint(user, groups)}"}


def test_new_query_text_is_not_persisted_but_audit_ids_and_counts_are(audit_corpus):
    client = audit_corpus
    raw_query = "cobalt memorandum applicant-private-number-918734"
    for _ in range(2):
        response = client.post("/query", json={"query": raw_query, "k": 2},
                               headers=headers("independent-reader", []))
        assert response.status_code == 200
        assert {row["doc_id"] for row in response.json()["results"]} == {
            "public-audit-control"}
    with SessionLocal() as session:
        rows = session.query(AuditLog).order_by(AuditLog.id).all()
        assert len(rows) == 2, "both initial and cached requests must be auditable"
        public_chunk_ids = {
            str(chunk.chunk_id) for chunk in session.query(ChunkACL).filter_by(
                doc_id="public-audit-control").all()
        }
        assert len(public_chunk_ids) == 1
        for row in rows:
            assert raw_query not in (row.query or "")
            assert "applicant-private-number-918734" not in (row.query or "")
            assert row.user_id == "independent-reader"
            assert row.groups == []
            assert row.returned_doc_ids == ["public-audit-control"]
            assert len(row.returned_chunk_ids) == 1
            assert {str(chunk_id) for chunk_id in row.returned_chunk_ids} == public_chunk_ids
            assert row.denied_count == 1
            assert row.fail_closed is False
            assert row.ts is not None


def test_security_audit_redacts_legacy_query_without_rewriting_the_old_record(audit_corpus):
    raw_query = "legacy-query-private-applicant-274198"
    with SessionLocal() as session:
        legacy = AuditLog(user_id="legacy-reader", groups=["legacy-team"],
                          query=raw_query, returned_chunk_ids=[405],
                          returned_doc_ids=["legacy-document"], denied_count=3,
                          fail_closed=False)
        session.add(legacy)
        session.commit()
        session.refresh(legacy)
        legacy_id = legacy.id
        legacy_ts = legacy.ts
    response = audit_corpus.get("/audit", headers=headers("security-reviewer", ["security"]))
    assert response.status_code == 200
    assert raw_query not in response.text
    assert "private-applicant-274198" not in response.text
    payload = response.json()
    assert len(payload["recent"]) == 1
    row = payload["recent"][0]
    assert row["user"] == "legacy-reader"
    assert row["groups"] == ["legacy-team"]
    assert row["returned_docs"] == ["legacy-document"]
    assert row["denied_count"] == 3
    assert row["fail_closed"] is False
    assert row["ts"]
    assert payload["denied_heatmap"] == [{
        "user": "legacy-reader", "queries_with_denials": 1, "total_denied_chunks": 3}]
    with SessionLocal() as session:
        assert session.query(AuditLog).count() == 1
        unchanged = session.get(AuditLog, legacy_id)
        assert unchanged.query == raw_query, "redaction must not rewrite append-only history"
        assert unchanged.ts == legacy_ts
        assert unchanged.returned_chunk_ids == [405]
        assert unchanged.returned_doc_ids == ["legacy-document"]
        assert unchanged.denied_count == 3


@pytest.mark.parametrize("warm_cache", [False, True])
def test_rebuild_required_alone_blocks_public_results_and_records_failure(
        audit_corpus, warm_cache):
    user = Principal(user_id="independent-rebuild-reader", groups=())
    query = "cobalt memorandum"
    if warm_cache:
        assert {row["doc_id"] for row in retrieve(query, user, k=2)["results"]} == {
            "public-audit-control"}
    with SessionLocal() as session:
        state = session.get(PermissionState, 1)
        assert state.pending is False
        assert state.rebuild_required is False
        # Deliberately preserve revision: an already warmed cache cannot bypass
        # an independent rebuild barrier, even in this inconsistent state.
        state.pending = False
        state.rebuild_required = True
        session.commit()
    assert retrieve(query, user, k=2)["results"] == []
    with SessionLocal() as session:
        state = session.get(PermissionState, 1)
        assert state.pending is False
        assert state.rebuild_required is True
        rows = session.query(AuditLog).order_by(AuditLog.id).all()
        assert len(rows) == 1 + int(warm_cache)
        last = rows[-1]
        assert last.user_id == "independent-rebuild-reader"
        assert last.fail_closed is True
        assert last.returned_doc_ids == []
        assert last.returned_chunk_ids == []
