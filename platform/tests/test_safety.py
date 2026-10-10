"""Platform safety: embedding fingerprint, PII redaction, ACL-safe degradation."""
import json
import logging
from unittest.mock import Mock

import pytest

from app import config, ingest, resilience, retrieval, store
from app.identity import Principal
from app.redact import redact
from app.vectorstore import client as qdrant
from conftest import CORPUS, CANARIES, USERS, auth, forbidden_canaries, reingest


def _principal(name):
    sub, groups, _ = USERS[name]
    return Principal(sub, tuple(groups))


@pytest.fixture(autouse=True)
def _fresh_breakers():
    resilience.reset_breakers()
    yield
    resilience.reset_breakers()


def _fingerprint_row():
    with store.SessionLocal() as s:
        row = s.get(store.IndexFingerprint, 1)
        return None if row is None else (row.backend, row.model, row.dim)


# ---- 6.1 embedding fingerprint ------------------------------------------------

def test_matching_fingerprint_retrieves(client):
    assert _fingerprint_row() == ("hash", "hash-ngram-v1", 384)
    out = retrieval.retrieve("vacation policy", _principal("alice"))
    assert out["source"] == "full_rag" and out["results"]


@pytest.mark.parametrize("attr,value", [
    ("EMBED_BACKEND", "st"),
    ("EMBED_MODEL", "other-model"),
    ("EMBED_DIM", 128),
])
def test_mismatched_fingerprint_fails_closed_with_prescriptive_log(
        client, monkeypatch, caplog, attr, value):
    embed = Mock()
    monkeypatch.setattr(retrieval, "embed_one", embed)
    monkeypatch.setattr(config, attr, value)
    with caplog.at_level(logging.ERROR, logger="permrag"):
        assert retrieval.retrieve("vacation policy", _principal("alice")) == retrieval.EMPTY_RESPONSE
    embed.assert_not_called()
    assert "index built with hash/hash-ngram-v1/384; configured " in caplog.text
    assert "reingest or restore config" in caplog.text
    assert client.get("/readyz").status_code == 503


def test_legacy_index_without_fingerprint_fails_closed(client, caplog):
    with store.SessionLocal() as s:
        s.query(store.IndexFingerprint).delete()
        s.commit()
    with caplog.at_level(logging.ERROR, logger="permrag"):
        assert retrieval.retrieve("vacation policy", _principal("alice")) == retrieval.EMPTY_RESPONSE
    assert "index has no embedding fingerprint; reingest" in caplog.text
    assert client.get("/readyz").status_code == 503
    reingest()
    assert client.get("/readyz").status_code == 200


def test_reingest_writes_new_fingerprint(client, monkeypatch):
    monkeypatch.setattr(config, "EMBED_DIM", 128)
    monkeypatch.setattr(config, "EMBED_MODEL", "m2")
    try:
        ingest.ingest_corpus(CORPUS)
        assert _fingerprint_row() == ("hash", "m2", 128)
        assert retrieval.retrieve("vacation policy", _principal("alice"))["results"]
    finally:
        monkeypatch.undo()
        reingest()


# ---- 6.2 PII redaction ----------------------------------------------------------

@pytest.mark.parametrize("raw,tag", [
    ("mail bob.smith+x@corp.example.com now", "EMAIL"),
    ("ssn 123-45-6789 on file", "SSN"),
    ("call (415) 555-0132 today", "PHONE"),
    ("call +1 415.555.0132 today", "PHONE"),
    ("card 4111 1111 1111 1111 expires", "CARD"),
    ("card 4111-1111-1111-1111 expires", "CARD"),
    ("card 378282246310005 amex", "CARD"),
])
def test_redact_each_type(raw, tag):
    out = redact(raw)
    assert out.count(f"[REDACTED:{tag}]") == 1
    for secret in ("bob.smith", "123-45-6789", "555-0132", "555.0132", "4111", "378282"):
        assert secret not in out


@pytest.mark.parametrize("keep", [
    "order 4111111111111112 shipped",
    "ticket 1234567890123456 open",
    "meeting 2026-10-10 at 14:30",
    "revenue $48,000,000 in 2026",
    "version 3.12.1 build 20261010",
    "extension 5550132",
])
def test_redact_keeps_non_pii(keep):
    assert redact(keep) == keep


def test_redact_is_exact_and_multi():
    assert redact("a@b.co and 123-45-6789") == "[REDACTED:EMAIL] and [REDACTED:SSN]"
    assert redact("") == ""


def test_ingest_redacts_before_embedding_and_storage(client, monkeypatch, tmp_path):
    corpus = tmp_path / "c.json"
    corpus.write_text(json.dumps([{"doc_id": "pii", "acl": ["*"], "sections": [
        {"text": "Reach jo@corp.example.com or 415-555-0132. SSN 123-45-6789. "
                 "Card 4111 1111 1111 1111. Order 4111111111111112."}]}]))
    embedded = []
    real = ingest.embed
    monkeypatch.setattr(ingest, "embed", lambda texts: embedded.extend(texts) or real(texts))
    try:
        ingest.ingest_corpus(corpus)
        expected = ("Reach [REDACTED:EMAIL] or [REDACTED:PHONE]. SSN [REDACTED:SSN]. "
                    "Card [REDACTED:CARD]. Order 4111111111111112.")
        assert embedded == [expected]
        payloads = [p.payload["text"] for p in qdrant().scroll(config.COLLECTION, limit=10)[0]]
        assert payloads == [expected]
        with store.SessionLocal() as s:
            assert [r.text for r in s.query(store.ChunkText).all()] == [expected]
    finally:
        monkeypatch.undo()
        reingest()


def test_output_is_redacted_even_if_index_or_generator_leak_pii(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_one", lambda q: [0.0])
    monkeypatch.setattr(retrieval, "search", lambda *a, **k: [
        {"chunk_id": 1, "doc_id": "handbook", "text": "legacy a@b.co row", "score": 0.9}])
    monkeypatch.setattr(retrieval, "search_unfiltered_count", lambda *a, **k: 0)
    monkeypatch.setattr(retrieval, "answer", lambda q, r: "call 123-45-6789 now")
    out = retrieval.retrieve("pii output probe", _principal("alice"))
    assert out["results"][0]["text"] == "legacy [REDACTED:EMAIL] row"
    assert out["answer"] == "call [REDACTED:SSN] now"


# ---- 8.3 circuit breaker ----------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _boom():
    raise RuntimeError("down")


def test_breaker_opens_after_threshold_and_half_opens_after_timeout():
    clock = Clock()
    b = resilience.CircuitBreaker("dep", threshold=3, reset_s=10, clock=clock)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            b.call(_boom)
    assert b.state == "closed"
    with pytest.raises(RuntimeError):
        b.call(_boom)
    assert b.state == "open"
    ok = Mock(return_value=1)
    with pytest.raises(resilience.BreakerOpen, match="dep"):
        b.call(ok)
    ok.assert_not_called()
    clock.t = 9.99
    assert b.state == "open"
    clock.t = 10.0
    assert b.state == "half_open"
    assert b.call(ok) == 1 and b.state == "closed"


def test_half_open_failure_reopens_and_success_resets_count():
    clock = Clock()
    b = resilience.CircuitBreaker("dep", threshold=2, reset_s=5, clock=clock)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            b.call(_boom)
    clock.t = 5.0
    with pytest.raises(RuntimeError):
        b.call(_boom)
    assert b.state == "open"
    clock.t = 9.99
    assert b.state == "open"
    clock.t = 10.0
    b.call(lambda: 1)
    with pytest.raises(RuntimeError):
        b.call(_boom)          # count restarted from zero after recovery
    assert b.state == "closed"


# ---- 8.3 degradation levels -----------------------------------------------------

def test_full_rag_marks_not_degraded(client):
    out = retrieval.retrieve("vacation policy", _principal("guest"))
    assert out["degraded"] is False and out["source"] == "full_rag"


def test_llm_down_gives_retrieval_only(client, monkeypatch):
    monkeypatch.setattr(retrieval, "answer", Mock(side_effect=RuntimeError("llm down")))
    out = retrieval.retrieve("vacation policy", _principal("guest"))
    assert out["degraded"] is True and out["source"] == "retrieval_only"
    assert out["results"] and out["answer"].startswith(out["results"][0]["text"])
    assert f"[{out['results'][0]['doc_id']}]" in out["answer"]


def test_embedder_down_gives_keyword_fallback_and_audits(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("embedder down")))
    out = retrieval.retrieve("vacation policy", _principal("guest"))
    assert out["degraded"] is True and out["source"] == "keyword_fallback"
    assert {r["doc_id"] for r in out["results"]} == {"handbook"}
    assert "vacation" in out["answer"].lower()
    with store.SessionLocal() as s:
        rec = s.query(store.AuditLog).one()
        assert rec.returned_doc_ids == ["handbook"] and rec.fail_closed is False


def test_vectorstore_down_gives_keyword_fallback(client, monkeypatch):
    monkeypatch.setattr(retrieval, "search", Mock(side_effect=RuntimeError("qdrant down")))
    out = retrieval.retrieve("on-call rotation pager", _principal("alice"))
    assert out["source"] == "keyword_fallback" and out["degraded"] is True
    assert [r["doc_id"] for r in out["results"]] == ["eng-oncall"]


def test_degraded_responses_are_not_cached(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("down")))
    assert retrieval.retrieve("vacation policy", _principal("guest"))["degraded"] is True
    monkeypatch.undo()
    resilience.reset_breakers()
    assert retrieval.retrieve("vacation policy", _principal("guest"))["source"] == "full_rag"


def test_keyword_fallback_failure_is_honest_unavailable(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(retrieval, "keyword_search", Mock(side_effect=RuntimeError("sql down")))
    out = retrieval.retrieve("vacation policy", _principal("guest"))
    assert out == {"results": [], "answer": "Service temporarily unavailable. Please retry shortly.",
                   "degraded": True, "source": "unavailable"}


def test_breaker_integration_stops_calling_dead_embedder_then_recovers(client, monkeypatch):
    clock = Clock()
    monkeypatch.setitem(resilience.BREAKERS, "embedder",
                        resilience.CircuitBreaker("embedder", 2, 30, clock))
    embed = Mock(side_effect=RuntimeError("down"))
    monkeypatch.setattr(retrieval, "embed_one", embed)
    for q in ("vacation one", "vacation two", "vacation three", "vacation four"):
        out = retrieval.retrieve(q, _principal("guest"))
        assert out["source"] == "keyword_fallback"
    assert embed.call_count == 2          # open breaker short-circuits the dead dependency
    clock.t = 31.0
    embed.side_effect = None
    embed.return_value = [0.0] * 384
    retrieval.retrieve("vacation five", _principal("guest"))
    assert embed.call_count == 3


@pytest.mark.parametrize("fault", ["embedder", "vectorstore", "llm"])
@pytest.mark.parametrize("name", sorted(USERS))
def test_no_forbidden_document_at_any_degradation_level(client, monkeypatch, fault, name):
    if fault == "embedder":
        monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("x")))
    elif fault == "vectorstore":
        monkeypatch.setattr(retrieval, "search", Mock(side_effect=RuntimeError("x")))
    else:
        monkeypatch.setattr(retrieval, "answer", Mock(side_effect=RuntimeError("x")))
    seen = ""
    for q in ("on-call rotation pager", "compensation bands salary", "executive compensation CEO package",
              "board minutes acquisition", "financial projections revenue target", "company strategy",
              "vacation policy", "database credentials vault", "salary spreadsheet"):
        out = retrieval.retrieve(q, _principal(name), k=20)
        assert out["degraded"] is True
        seen += json.dumps(out)
    for canary in forbidden_canaries(name):
        assert canary not in seen
    # not vacuous: readable canaries ARE reachable at this level
    sub, groups, readable = USERS[name]
    if "eng-oncall" in readable:
        assert CANARIES["eng-oncall"] in seen
    if "hr-salaries" in readable:
        assert CANARIES["hr-salaries"] in seen
    if "finance" in groups:
        assert CANARIES["company-strategy(finance section)"] in seen


def test_keyword_fallback_enforces_and_across_levels(client, monkeypatch, tmp_path):
    corpus = tmp_path / "h.json"
    corpus.write_text(json.dumps([{"doc_id": "tri", "acl": ["group:a", "group:b"], "sections": [
        {"acl": ["group:a", "group:c"], "paragraphs": [
            {"text": "zebra alpha both", "acl": ["group:a"]},
            {"text": "zebra bravo only", "acl": ["group:b"]},
            {"text": "zebra charlie open"}]}]}]))
    try:
        ingest.ingest_corpus(corpus)
        monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("x")))

        def texts(p):
            out = retrieval.retrieve("zebra", p, k=20)
            assert out["source"] == "keyword_fallback"
            return sorted(r["text"] for r in out["results"])
        assert texts(Principal("u", ("a",))) == ["zebra alpha both", "zebra charlie open"]
        assert texts(Principal("u", ("b",))) == []      # fails section level
        assert texts(Principal("u", ("c",))) == []      # fails document level
        assert texts(Principal("u", ("a", "b"))) == [
            "zebra alpha both", "zebra bravo only", "zebra charlie open"]
    finally:
        monkeypatch.undo()
        reingest()


def test_keyword_fallback_honors_revocation_after_sync(client, monkeypatch):
    from app.sync import sync_once
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("x")))
    alice = _principal("alice")
    assert retrieval.retrieve("on-call rotation pager", alice)["results"]
    rows = [json.loads(line) for line in open(config.PERMISSIONS_SOURCE)]
    with open(config.PERMISSIONS_SOURCE, "w") as f:
        for r in rows:
            if r["doc_id"] == "eng-oncall":
                r["acl"] = ["group:nobody"]
            f.write(json.dumps(r) + "\n")
    try:
        sync_once()
        out = retrieval.retrieve("on-call rotation pager", alice)
        assert out["results"] == [] and CANARIES["eng-oncall"] not in json.dumps(out)
    finally:
        monkeypatch.undo()
        reingest()


def test_permission_uncertainty_stays_fail_closed_not_degraded(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("x")))
    keyword = Mock()
    monkeypatch.setattr(retrieval, "keyword_search", keyword)
    with store.SessionLocal() as s:
        s.query(store.PermissionState).update({"pending": True})
        s.commit()
    try:
        assert retrieval.retrieve("vacation policy", _principal("guest")) == retrieval.EMPTY_RESPONSE
    finally:
        with store.SessionLocal() as s:
            s.query(store.PermissionState).update({"pending": False})
            s.commit()
    keyword.assert_not_called()


def test_query_endpoint_exposes_degraded_and_source(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("x")))
    body = client.post("/query", json={"query": "vacation policy"}, headers=auth("guest")).json()
    assert body["degraded"] is True and body["source"] == "keyword_fallback"


def test_keyword_fallback_with_only_stopwords_returns_nothing(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("x")))
    out = retrieval.retrieve("what is the", _principal("alice"))
    assert out["source"] == "keyword_fallback" and out["results"] == []
    assert out["answer"] == "No results found."


def test_deleted_document_text_is_removed_from_sql_mirror(client):
    from app.sync import sync_once
    rows = [json.loads(line) for line in open(config.PERMISSIONS_SOURCE)]
    rows.append({"doc_id": "handbook", "deleted": True})
    rows = [r for r in rows if not (r["doc_id"] == "handbook" and not r.get("deleted"))]
    with open(config.PERMISSIONS_SOURCE, "w") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")
    try:
        with store.SessionLocal() as s:
            before = s.query(store.ChunkText).count()
        sync_once()
        with store.SessionLocal() as s:
            assert s.query(store.ChunkACL).filter_by(doc_id="handbook").count() == 0
            assert s.query(store.ChunkText).count() < before
            assert not [t for t in s.query(store.ChunkText).all() if "vacation" in t.text.lower()]
    finally:
        reingest()


def test_luhn_doubling_of_five_redacts_valid_mastercard():
    # 5555555555554444 is a published Luhn-valid test number; its doubled digits include 5s
    assert redact("card 5555555555554444 on file") == "card [REDACTED:CARD] on file"
    assert redact("id 5555555555554445 on file") == "id 5555555555554445 on file"


def test_keyword_scores_are_matched_fraction_of_query_terms(client):
    with store.SessionLocal() as s:
        hits = resilience.keyword_search(s, "vacation policy days zzzunmatched", _principal("guest").principals, 5)
    assert hits
    terms = resilience._terms("vacation policy days zzzunmatched")
    for h in hits:
        assert h["score"] == len(terms & resilience._terms(h["text"])) / len(terms)
        assert 0 < h["score"] < 1
