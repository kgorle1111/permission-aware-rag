"""Regression tests for identity and availability boundaries."""
import copy
import datetime as dt
import json

import pytest

from conftest import CORPUS, _key, auth


def _signed_token(claims):
    import jwt

    return jwt.encode(claims, _key, algorithm="RS256")


def _valid_claims(**overrides):
    from app import config

    claims = {
        "sub": "user@example.com",
        "groups": ["eng"],
        "aud": config.JWT_AUDIENCE,
        "iat": dt.datetime.now(dt.timezone.utc),
        "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
    }
    claims.update(overrides)
    return claims


@pytest.mark.parametrize(
    "claims,issuer",
    [
        (_valid_claims(sub="  "), None),
        (_valid_claims(groups=["eng", 17]), None),
        ({k: v for k, v in _valid_claims().items() if k != "exp"}, None),
        (_valid_claims(iss="untrusted-issuer"), "trusted-issuer"),
    ],
    ids=["empty-subject", "mixed-group-types", "missing-expiry", "issuer-mismatch"],
)
def test_identity_claim_contract_rejects_malformed_or_untrusted_tokens(
    client, monkeypatch, claims, issuer
):
    from app import config

    if issuer is not None:
        monkeypatch.setattr(config, "JWT_ISSUER", issuer)
    token = _signed_token(claims)
    response = client.post("/query", json={"query": "salary bands"},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_pending_permission_state_bypasses_previously_cached_answer(client):
    from app.store import PermissionState, SessionLocal

    query = "what are the salary bands?"
    initial = client.post("/query", json={"query": query}, headers=auth("bob"))
    assert initial.status_code == 200
    assert any(row["doc_id"] == "hr-salaries" for row in initial.json()["results"])

    try:
        with SessionLocal() as session:
            state = session.get(PermissionState, 1)
            state.pending = True
            session.commit()

        blocked = client.post("/query", json={"query": query}, headers=auth("bob"))
        assert blocked.status_code == 200
        assert blocked.json() == {"results": [], "answer": "No results found."}
    finally:
        # Restore the durable state so this fixture does not leak into later tests.
        with SessionLocal() as session:
            state = session.get(PermissionState, 1)
            state.pending = False
            session.commit()


def test_cache_is_bounded_expires_and_returns_isolated_copies(client, monkeypatch):
    from types import SimpleNamespace

    from app import config, retrieval
    from app.identity import Principal

    retrieval.clear_cache()
    monkeypatch.setattr(config, "CACHE_MAX_ENTRIES", 1)
    monkeypatch.setattr(config, "CACHE_TTL_S", 10)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(retrieval, "time", SimpleNamespace(monotonic=lambda: clock.now))
    searches = []

    def search(_vector, _principals, top_k):
        searches.append(top_k)
        return [{"chunk_id": len(searches), "doc_id": "demo", "text": f"hit-{len(searches)}",
                 "score": 0.9}]

    monkeypatch.setattr(retrieval, "embed_one", lambda _query: [0.0])
    monkeypatch.setattr(retrieval, "search", search)
    monkeypatch.setattr(retrieval, "search_unfiltered_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(retrieval, "answer", lambda _query, _results: "answer")
    principal = Principal("cache-user", ("eng",))

    first = retrieval.retrieve("first query", principal)
    first["results"][0]["text"] = "caller mutation"
    cached = retrieval.retrieve("first query", principal)
    assert cached["results"][0]["text"] == "hit-1"
    assert len(searches) == 1

    retrieval.retrieve("second query", principal)
    retrieval.retrieve("first query", principal)
    assert len(searches) == 3, "bounded cache should evict the older query"

    clock.now += 11
    retrieval.retrieve("first query", principal)
    assert len(searches) == 4, "expired entries should be recomputed"
    retrieval.clear_cache()


def test_duplicate_document_reingestion_preserves_existing_index(client, tmp_path):
    from app.store import ChunkACL, PermissionState, SessionLocal
    from app.vectorstore import client as vector_client
    from app import config
    from app.ingest import ingest_corpus

    docs = json.loads(CORPUS.read_text())
    invalid = copy.deepcopy(docs)
    invalid.append(copy.deepcopy(invalid[0]))
    candidate = tmp_path / "duplicate-docs.json"
    candidate.write_text(json.dumps(invalid))

    with SessionLocal() as session:
        before_rows = [(r.chunk_id, r.doc_id, r.acl) for r in session.query(ChunkACL).order_by(ChunkACL.chunk_id)]
        before_state = session.get(PermissionState, 1)
        before_revision = before_state.revision
        before_pending = before_state.pending
    before_points = vector_client().count(config.COLLECTION, exact=True).count

    with pytest.raises(ValueError, match="unique"):
        ingest_corpus(candidate)

    with SessionLocal() as session:
        after_rows = [(r.chunk_id, r.doc_id, r.acl) for r in session.query(ChunkACL).order_by(ChunkACL.chunk_id)]
        after_state = session.get(PermissionState, 1)
        assert after_state.revision == before_revision
        assert after_state.pending is before_pending
    after_points = vector_client().count(config.COLLECTION, exact=True).count
    assert after_rows == before_rows
    assert after_points == before_points
