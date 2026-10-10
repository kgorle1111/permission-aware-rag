"""Kills for mutants that survived the safety-branch merge (mutmut 3.8)."""
import json
import logging
from unittest.mock import Mock

import pytest
from sqlalchemy import event

from app import config, resilience, retrieval, store, sync
from app import observability as obs
from app.identity import Principal
from app.ingest import ingest_corpus
from app.resilience import BreakerOpen, CircuitBreaker
from conftest import CORPUS, reingest


class Boom(Exception):
    pass


@pytest.fixture(autouse=True)
def _fresh_breakers():
    resilience.reset_breakers()
    yield
    resilience.reset_breakers()


def _spy_audit(monkeypatch):
    real = retrieval.write_audit
    spy = Mock(side_effect=real)
    monkeypatch.setattr(retrieval, "write_audit", spy)
    return spy


def _healthy(monkeypatch):
    monkeypatch.setattr(retrieval, "embed_one", Mock(return_value=[0.1]))
    monkeypatch.setattr(retrieval, "search", Mock(return_value=[]))
    monkeypatch.setattr(retrieval, "search_unfiltered_count", Mock(return_value=0))
    monkeypatch.setattr(retrieval, "answer", Mock(return_value="ok"))


# sync_once__161: deleting one doc's chunk text must not wipe other chunks' text
def test_sync_delete_keeps_other_documents_chunk_text(client, tmp_path):
    with store.SessionLocal() as s:
        gone = {r.chunk_id for r in s.query(store.ChunkACL).filter_by(doc_id="handbook")}
        before = {t.chunk_id for t in s.query(store.ChunkText)}
    assert gone and before - gone
    src = tmp_path / "c.jsonl"
    src.write_text(json.dumps({"doc_id": "handbook", "deleted": True}))
    try:
        assert sync.sync_once(src) == ["handbook"]
        with store.SessionLocal() as s:
            assert {t.chunk_id for t in s.query(store.ChunkText)} == before - gone
    finally:
        reingest()


# ingest_corpus__67/71: the fingerprint row must be written with explicit id=1
def test_ingest_writes_fingerprint_with_explicit_id_one(client):
    seen = []
    def grab(mapper, conn, target):
        seen.append(target.id)
    event.listen(store.IndexFingerprint, "before_insert", grab)
    try:
        ingest_corpus(CORPUS)
    finally:
        event.remove(store.IndexFingerprint, "before_insert", grab)
        reingest()
    assert seen == [1]


# resilience._make_breakers 1-3, 7-12
def test_make_breakers_names_and_configured_limits():
    b = resilience._make_breakers()
    assert list(b) == ["embedder", "vectorstore", "llm"]
    for name, br in b.items():
        assert br.name == name
        assert br.threshold == config.BREAKER_THRESHOLD
        assert br.reset_s == config.BREAKER_RESET_S


# resilience._terms 7, 9
def test_terms_split_on_punctuation_and_whitespace():
    assert resilience._terms("Vacation-days,policy! HR/payroll") == {
        "vacation", "days", "policy", "hr", "payroll"}


# CircuitBreaker.call 7
def test_breaker_call_forwards_kwargs():
    got = {}
    def fn(a, b=None):
        got.update(a=a, b=b)
        return "r"
    assert CircuitBreaker("x", 3, 1.0).call(fn, 1, b=2) == "r"
    assert got == {"a": 1, "b": 2}


# store.fingerprint_problem 8, 19
def test_fingerprint_problem_exact_messages(client):
    with store.SessionLocal() as s:
        assert store.fingerprint_problem(s) is None
        row = s.get(store.IndexFingerprint, 1)
        row.backend, row.model, row.dim = "other", "m", 7
        s.flush()
        backend, model, dim = store.fingerprint()
        assert store.fingerprint_problem(s) == (
            f"index built with other/m/7; configured {backend}/{model}/{dim}; "
            "reingest or restore config")
        s.rollback()
        s.query(store.IndexFingerprint).delete()
        assert store.fingerprint_problem(s) == "index has no embedding fingerprint; reingest"
        s.rollback()


# _compute 24-29, 34, 39: dependency failure -> keyword fallback
def test_fallback_logs_exception_type_and_forwards_k_and_zero_denied(client, monkeypatch, caplog):
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=Boom("x")))
    kw = Mock(return_value=[])
    monkeypatch.setattr(retrieval, "keyword_search", kw)
    p = Principal("u", ("g",))
    with caplog.at_level(logging.WARNING, logger="permrag"), store.SessionLocal() as s:
        resp, chunks, docs, denied = retrieval._compute(s, "q", p, 7, obs.new_record())
    kw.assert_called_once_with(s, "q", p.principals, 7)
    assert denied == 0 and resp["source"] == "keyword_fallback" and resp["degraded"] is True
    assert [r.getMessage() for r in caplog.records] == [
        "dependency unavailable (Boom); using keyword fallback"]


# _compute 40-42, 54: keyword fallback also fails
def test_unavailable_when_fallback_fails(client, monkeypatch, caplog):
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=Boom("x")))
    monkeypatch.setattr(retrieval, "keyword_search", Mock(side_effect=Boom("y")))
    with caplog.at_level(logging.WARNING, logger="permrag"), store.SessionLocal() as s:
        resp, chunks, docs, denied = retrieval._compute(s, "q", Principal("u"), 4, obs.new_record())
    assert resp["source"] == "unavailable" and denied == 0 and chunks == [] and docs == []
    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errs] == ["keyword fallback failed"]
    assert errs[0].exc_info and errs[0].exc_info[0] is Boom


# _compute 79, 80: fallback answers are extractive, never sent to the LLM
def test_keyword_fallback_never_calls_llm(client, monkeypatch):
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=Boom("x")))
    monkeypatch.setattr(retrieval, "keyword_search", Mock(return_value=[
        {"chunk_id": 1, "doc_id": "d", "text": "alpha beta", "score": 1.0}]))
    llm = Mock(return_value="LLM-TEXT")
    monkeypatch.setattr(retrieval, "answer", llm)
    with store.SessionLocal() as s:
        resp = retrieval._compute(s, "alpha", Principal("u"), 4, obs.new_record())[0]
    llm.assert_not_called()
    assert resp["answer"] == "alpha beta [d]"


# _compute 93-98: LLM failure -> retrieval_only
def test_generation_failure_logs_exception_type(client, monkeypatch, caplog):
    _healthy(monkeypatch)
    monkeypatch.setattr(retrieval, "answer", Mock(side_effect=Boom("x")))
    with caplog.at_level(logging.WARNING, logger="permrag"), store.SessionLocal() as s:
        resp = retrieval._compute(s, "q", Principal("u"), 4, obs.new_record())[0]
    assert resp["source"] == "retrieval_only" and resp["degraded"] is True
    assert [r.getMessage() for r in caplog.records] == [
        "generation unavailable (Boom); retrieval-only answer"]


# retrieve 39, 55, 56: audit receives the query and the unavailable flag
def test_retrieve_passes_query_and_unavailable_flag_to_audit(client, monkeypatch):
    _healthy(monkeypatch)
    spy = _spy_audit(monkeypatch)
    p = Principal("u")
    retrieval.retrieve("healthy question", p)
    args = spy.call_args.args
    assert args[1] == "healthy question" and spy.call_args.kwargs["session"] is not None
    assert args[5] is False
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=Boom("x")))
    monkeypatch.setattr(retrieval, "keyword_search", Mock(side_effect=Boom("y")))
    retrieval.retrieve("outage question", p)
    args = spy.call_args.args
    assert args[1] == "outage question" and args[5] is True
    with store.SessionLocal() as s:
        assert s.query(store.AuditLog).order_by(store.AuditLog.id.desc()).first().fail_closed is True


# retrieve 79: fail-closed audit also receives the query
def test_failed_retrieve_passes_query_to_fail_closed_audit(client, monkeypatch):
    spy = _spy_audit(monkeypatch)
    assert retrieval.retrieve("bad k question", Principal("u"), k=0) == retrieval.EMPTY_RESPONSE
    spy.assert_called_once()
    args = spy.call_args.args
    assert args[1] == "bad k question" and args[2:] == ([], [], 0, True)


# retrieve 71: LRU eviction drops the OLDEST entry
def test_cache_evicts_oldest_entry_first(client, monkeypatch):
    _healthy(monkeypatch)
    monkeypatch.setattr(config, "CACHE_MAX_ENTRIES", 2)
    p = Principal("u")
    for q in ("q1", "q2", "q3"):
        retrieval.retrieve(q, p)
    assert [k[2] for k in retrieval._cache] == ["q2", "q3"]
