"""Behavioral contracts for identity verification and Qdrant query boundaries."""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import config, identity, ingest, retrieval, vectorstore
from app.identity import Principal
from conftest import mint


def test_local_public_key_is_loaded_cold_then_reused(monkeypatch):
    monkeypatch.setattr(config, "JWKS_URL", "")
    monkeypatch.setattr(identity, "_public_key_cache", None)
    token = mint("cache-check@example.test", ["staff"])

    assert identity.verify_token(token) == Principal("cache-check@example.test", ("staff",))
    loaded_key = identity._public_key_cache
    assert isinstance(loaded_key, str) and "BEGIN PUBLIC KEY" in loaded_key
    assert identity.verify_token(token) == Principal("cache-check@example.test", ("staff",))
    assert identity._public_key_cache == loaded_key


def test_jwks_client_is_constructed_for_configured_url(monkeypatch):
    expected_url = "https://identity.example.test/.well-known/jwks.json"
    calls = []

    class FakeJwksClient:
        def __init__(self, url):
            calls.append(url)

        def get_signing_key_from_jwt(self, _token):
            return SimpleNamespace(key=Path(config.JWT_PUBLIC_KEY_PATH).read_text())

    monkeypatch.setattr(config, "JWKS_URL", expected_url)
    monkeypatch.setattr(identity.jwt, "PyJWKClient", FakeJwksClient)
    monkeypatch.setattr(identity, "_jwks_client", None)

    assert identity.verify_token(mint("jwks@example.test", [])) == Principal(
        "jwks@example.test", ()
    )
    assert calls == [expected_url]


def test_signed_token_without_optional_groups_claim_defaults_to_no_groups(monkeypatch):
    monkeypatch.setattr(config, "JWKS_URL", "")
    monkeypatch.setattr(identity, "_public_key_cache", None)
    token = identity.jwt.encode(
        {
            "sub": "ungrouped@example.test",
            "aud": config.JWT_AUDIENCE,
            "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5),
        },
        Path(config.JWT_PRIVATE_KEY_PATH).read_text(),
        algorithm="RS256",
    )

    assert identity.verify_token(token) == Principal("ungrouped@example.test", ())


def test_invalid_token_error_remains_a_401_with_sanitized_prefix():
    with pytest.raises(HTTPException) as error:
        identity.verify_token("not.a.jwt")
    assert error.value.status_code == 401
    assert error.value.detail.startswith("invalid token:")


def test_malformed_groups_have_stable_fail_closed_detail():
    with pytest.raises(HTTPException) as error:
        identity.verify_token(mint("malformed@example.test", groups="staff"))
    assert error.value.status_code == 401
    assert error.value.detail == "invalid token: groups claim malformed"


def test_missing_bearer_has_stable_401_detail_and_header_lookup_is_case_insensitive():
    with pytest.raises(HTTPException) as error:
        identity.principal_from_request(Request({"type": "http", "headers": []}))
    assert error.value.status_code == 401
    assert error.value.detail == "missing bearer token"

    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {mint('header@example.test', [])}".encode())],
        }
    )
    assert request.headers.get("AUTHORIZATION", "").startswith("Bearer ")
    assert identity.principal_from_request(request) == Principal("header@example.test", ())


def test_acl_principal_value_can_contain_colons_after_the_type_prefix():
    assert ingest.validate_acl(["user::"]) == ["user::"]
    assert Principal(":").principals == ["user::", "*"]


def test_retrieval_uses_configured_default_limit_when_k_is_omitted(
    client, monkeypatch
):
    limits = []
    monkeypatch.setattr(retrieval, "embed_one", lambda _query: [0.0] * config.EMBED_DIM)
    monkeypatch.setattr(
        retrieval,
        "search",
        lambda _vector, _principals, top_k: limits.append(top_k) or [],
    )
    monkeypatch.setattr(retrieval, "search_unfiltered_count", lambda *_a, **_k: 0)
    monkeypatch.setattr(retrieval, "answer", lambda _query, _results: "No results found.")

    assert retrieval.retrieve("default limit contract", Principal("reader@example.test")) == {
        "results": [],
        "answer": "No results found.",
        "degraded": False,
        "source": "full_rag",
    }
    assert limits == [config.TOP_K]


def test_vector_search_passes_acl_filter_limit_and_payload_options(monkeypatch):
    captured = {}

    class FakeClient:
        def query_points(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                points=[SimpleNamespace(id=41, score=0.81,
                                        payload={"doc_id": "handbook", "text": "visible"})]
            )

    monkeypatch.setattr(vectorstore, "client", lambda: FakeClient())
    result = vectorstore.search([0.2, 0.8], ["user:reader@example.test", "*"], 3)

    assert result == [
        {"chunk_id": 41, "score": 0.81, "doc_id": "handbook", "text": "visible"}
    ]
    assert captured["query"] == [0.2, 0.8]
    assert captured["limit"] == 3
    assert captured["score_threshold"] == config.MIN_SCORE
    assert captured["with_payload"] is True
    acl_filter = captured["query_filter"].must[0]
    assert [c.key for c in captured["query_filter"].must] == ["acl_doc", "acl_section", "acl_para"]
    assert all(c.match.any == ["user:reader@example.test", "*"] for c in captured["query_filter"].must)
    assert acl_filter.key == "acl_doc"
    assert acl_filter.match.any == ["user:reader@example.test", "*"]


def test_upsert_is_durable_before_permission_state_can_be_committed(monkeypatch):
    captured = {}

    class FakeClient:
        def upsert(self, collection_name, **kwargs):
            captured["collection_name"] = collection_name
            captured.update(kwargs)

    monkeypatch.setattr(vectorstore, "client", lambda: FakeClient())
    chunks = [
        {
            "id": 7,
            "doc_id": "confidential-plan",
            "text": "Private text",
            "acl": ["group:finance"],
            "acl_doc": ["group:finance"], "acl_section": ["*"], "acl_para": ["*"],
            "vector": [0.3, 0.7],
        }
    ]
    vectorstore.upsert_chunks(chunks)

    assert captured["collection_name"] == config.COLLECTION
    assert captured["wait"] is True
    points = captured["points"]
    assert len(points) == 1
    assert points[0].id == 7
    assert points[0].vector == [0.3, 0.7]
    assert points[0].payload == {
        "doc_id": "confidential-plan",
        "text": "Private text",
        "acl": ["group:finance"],
        "acl_doc": ["group:finance"], "acl_section": ["*"], "acl_para": ["*"],
    }


def test_unfiltered_audit_query_is_relevant_limited_and_returns_only_denied_count(
    monkeypatch,
):
    captured = {}
    hits = [
        SimpleNamespace(payload={"acl_doc": ["group:hr"], "acl_section": ["group:hr"], "acl_para": ["group:hr"]}),
        SimpleNamespace(payload={"acl_doc": ["group:eng"], "acl_section": ["group:eng"], "acl_para": ["group:eng"]}),
    ]

    class FakeClient:
        def query_points(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(points=hits)

    monkeypatch.setattr(vectorstore, "client", lambda: FakeClient())
    denied_count = vectorstore.search_unfiltered_count(
        [1.0, 0.0], ["group:hr", "user:reader@example.test"], 2
    )

    assert denied_count == 1
    assert captured["query"] == [1.0, 0.0]
    assert captured["limit"] == 2
    assert captured["score_threshold"] == config.MIN_SCORE
    assert captured["with_payload"] == ["acl_doc", "acl_section", "acl_para"]
    assert "query_filter" not in captured


def test_unfiltered_audit_treats_missing_acl_payload_as_denied(monkeypatch):
    class FakeClient:
        def query_points(self, **_kwargs):
            return SimpleNamespace(
                points=[SimpleNamespace(payload={"doc_id": "legacy-no-acl"})]
            )

    monkeypatch.setattr(vectorstore, "client", lambda: FakeClient())
    assert vectorstore.search_unfiltered_count([0.1], ["group:hr"], 1) == 1


def test_acl_update_reads_no_document_data_and_waits_for_visibility(monkeypatch):
    captured = {}

    class FakeClient:
        def retrieve(self, collection_name, **kwargs):
            captured["retrieve_collection"] = collection_name
            captured["retrieve"] = kwargs
            return [object()]

        def set_payload(self, **kwargs):
            captured["set_payload"] = kwargs

    monkeypatch.setattr(vectorstore, "client", lambda: FakeClient())
    vectorstore.update_chunk_acl(91, ["group:hr"])

    assert captured["retrieve_collection"] == config.COLLECTION
    assert captured["retrieve"] == {
        "ids": [91],
        "with_payload": False,
        "with_vectors": False,
    }
    assert captured["set_payload"] == {
        "collection_name": config.COLLECTION,
        "payload": {"acl": ["group:hr"], "acl_doc": ["group:hr"]},
        "points": [91],
        "wait": True,
    }


def test_acl_update_missing_point_preserves_full_reingestion_error(monkeypatch):
    class FakeClient:
        def retrieve(self, *_args, **_kwargs):
            return []

        def set_payload(self, **_kwargs):
            pytest.fail("must not write an ACL for a missing point")

    monkeypatch.setattr(vectorstore, "client", lambda: FakeClient())
    with pytest.raises(RuntimeError) as error:
        vectorstore.update_chunk_acl(91, ["group:hr"])
    assert str(error.value) == "indexed chunk missing; full reingestion required"


def test_remote_document_delete_waits_for_exact_document_filter(monkeypatch):
    captured = {}

    class FakeClient:
        def delete(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(vectorstore, "client", lambda: FakeClient())
    vectorstore.delete_doc("drive-doc-identifier")

    assert captured["collection_name"] == config.COLLECTION
    assert captured["wait"] is True
    selector = captured["points_selector"].must[0]
    assert selector.key == "doc_id"
    assert selector.match.value == "drive-doc-identifier"
