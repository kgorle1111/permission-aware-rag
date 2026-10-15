"""Mutation-survivor kills for the retrieval-model PR (hybrid search, sparse cache, reranker).

No model is downloaded: every model call is a fake behind embeddings._load_fastembed.
"""
import hashlib
import logging
import math
import sqlite3

import pytest
from qdrant_client.models import FieldCondition, HasIdCondition

from app import config, embeddings, rerank, retrieval, vectorstore
from conftest import reingest
from test_retrieval_models import (  # noqa: F401  (fixtures are collected by name)
    _fresh_breakers, _principal, cache_path, fake_models, hybrid_index,
    reranked)

DIM = 384


def _unit(cos, axis):
    """Unit vector at cosine `cos` to e0, tilted along e<axis>."""
    v = [0.0] * DIM
    v[0], v[axis] = cos, math.sqrt(1 - cos * cos)
    return v


def _pt(i, doc, cos, sparse, acl):
    return {"id": i, "doc_id": doc, "text": doc, "acl": acl, "acl_doc": acl, "acl_section": ["*"],
            "acl_para": ["*"], "vector": _unit(cos, i), "sparse": sparse}


# Query = e0 with sparse term 1. Visible dense order: A > B > C; only A and C carry the
# sparse term (C more strongly). Hidden chunks beat every visible chunk on dense AND sparse.
QUERY, QSPARSE = _unit(1.0, 1), ([1], [1.0])
VISIBLE = [_pt(1, "A", 0.90, ([1], [1.0]), ["*"]),
           _pt(2, "B", 0.80, ([2], [1.0]), ["*"]),
           _pt(3, "C", 0.70, ([1], [5.0]), ["*"])]
HIDDEN = [_pt(10, "H1", 0.99, ([1], [9.0]), ["group:secret"]),
          _pt(11, "H2", 0.98, ([1], [9.0]), ["group:secret"])]


@pytest.fixture()
def hybrid_store(client, monkeypatch):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")

    def load(points):
        vectorstore.reset_collection()
        vectorstore.upsert_chunks(points)
    yield load
    monkeypatch.undo()
    reingest()


def _run(load, points, top_k=1):
    load(points)
    return [(h["doc_id"], h["score"]) for h in vectorstore.search(QUERY, ["*"], top_k, sparse=QSPARSE)]


def test_hybrid_results_do_not_depend_on_hidden_chunks_in_the_inner_dense_candidate_pool(hybrid_store):
    # Behavioural invariant (S1-style side channel): hidden chunks that are dense-closer than every
    # visible chunk must not change a visible user's results or scores. NOTE: embedded Qdrant
    # propagates the outer/prefetch filters down to the leaves, so dropping the inner filter is not
    # observable here; test_hybrid_nested_dense_prefetch_carries_the_acl_filter pins the request.
    without = _run(hybrid_store, VISIBLE)
    with_hidden = _run(hybrid_store, VISIBLE + HIDDEN)
    assert without == with_hidden
    [(doc, score)] = with_hidden
    assert doc == "A"
    # A is in BOTH fused lists (dense rank 1 and sparse rank 1): twice a single-list contribution.
    hybrid_store(VISIBLE)
    [none] = vectorstore.search(QUERY, ["*"], 1, sparse=([999], [1.0]))  # sparse branch empty
    assert none["doc_id"] == "A" and score == pytest.approx(2 * none["score"])


def test_hybrid_candidate_pool_is_exactly_twice_top_k(hybrid_store):
    # C is the 3rd visible chunk by dense score: outside the 2*top_k pool, so it must not enter the
    # sparse branch and displace A's sparse rank (3*top_k, the default prefetch limit or no limit
    # would admit it, since C has the strongest sparse weight).
    [(doc, score)] = _run(hybrid_store, VISIBLE)
    assert doc == "A"
    hybrid_store(VISIBLE)
    [single] = vectorstore.search(QUERY, ["*"], 1, sparse=([999], [1.0]))
    assert score == pytest.approx(2 * single["score"])


def _kws_of_hybrid_search(monkeypatch, principals):
    real = vectorstore.client()
    seen = []
    original = real.query_points
    monkeypatch.setattr(real, "query_points", lambda **kw: seen.append(kw) or original(**kw))
    vectorstore.search(QUERY, principals, 3, sparse=QSPARSE)
    return seen


def test_every_hybrid_query_carries_the_acl_filter(hybrid_store, monkeypatch):
    # Dense and sparse run as separate queries; each candidate list must be built from readable
    # rows only, so every query sent (not just the outer one) carries the full per-level ACL filter.
    # The sparse query is additionally restricted to the dense candidate ids.
    hybrid_store(VISIBLE + HIDDEN)
    principals = ["*", "user:guest@external.com"]
    dense, sparse = _kws_of_hybrid_search(monkeypatch, principals)
    expected = sorted((lvl, tuple(principals)) for lvl in vectorstore.ACL_LEVELS)
    for kw in (dense, sparse):
        assert "prefetch" not in kw
        acl = [c for c in kw["query_filter"].must if isinstance(c, FieldCondition)]
        assert sorted((c.key, tuple(c.match.any)) for c in acl) == expected
    assert dense["using"] == "dense" and dense["query"] == QUERY
    assert dense["score_threshold"] == config.MIN_SCORE and dense["limit"] == 2 * 3 + 1
    [only_ids] = [c for c in sparse["query_filter"].must if isinstance(c, HasIdCondition)]
    assert sorted(only_ids.has_id) == [1, 2, 3]
    assert sparse["using"] == "sparse" and sparse["limit"] == 3


# ---- exact error messages (a substring/regex match lets "XXmsgXX" survive) ----------------------

def _message(exc_info):
    return str(exc_info.value)


def test_dense_dimension_mismatch_message_is_exact(monkeypatch, fake_models):
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    monkeypatch.setattr(config, "EMBED_DIM", 8)
    with pytest.raises(ValueError) as e:
        embeddings._encode(["alpha"])
    assert _message(e) == "embedding dimension mismatch"


def test_unknown_model_kind_message_is_exact(monkeypatch):
    monkeypatch.setattr(embeddings, "_fe_models", {})
    with pytest.raises(ValueError) as e:
        embeddings._load_fastembed("other", "m")
    assert _message(e) == "unknown model kind"


def test_sparse_backend_messages_are_exact(monkeypatch):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "none")
    with pytest.raises(ValueError) as e:
        embeddings._sparse_encode(["x"])
    assert _message(e) == "invalid sparse backend"
    monkeypatch.setattr(config, "SPARSE_BACKEND", "bm42")
    with pytest.raises(ValueError) as e:
        embeddings.sparse_enabled()
    assert _message(e) == "invalid sparse backend"


def test_reranker_backend_message_is_exact(monkeypatch):
    monkeypatch.setattr(config, "RERANKER", "cohere")
    with pytest.raises(ValueError) as e:
        rerank.rerank("q", [{"chunk_id": 1, "doc_id": "x", "text": "t", "score": 0.1}], 1)
    assert _message(e) == "invalid reranker backend"


# ---- sparse model wiring and cache ---------------------------------------------------------------

def test_sparse_encode_loads_the_configured_sparse_model(monkeypatch, fake_models):
    asked = []
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    monkeypatch.setattr(embeddings, "_load_fastembed", lambda kind, name: asked.append((kind, name)) or fake_models["sparse"])
    embeddings._sparse_encode(["alpha"])
    monkeypatch.setattr(config, "SPARSE_MODEL", "other/sparse")
    embeddings._sparse_encode(["alpha"])
    assert asked == [("sparse", "prithivida/Splade_PP_en_v1"), ("sparse", "other/sparse")]


def test_sparse_cache_rows_are_stored_under_kind_sparse_and_the_splade_model_id(monkeypatch, fake_models, cache_path):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    monkeypatch.setattr(embeddings, "_fastembed_version", lambda: "9.9")
    embeddings.sparse_embed(["alpha beta"])
    with sqlite3.connect(cache_path) as db:
        rows = db.execute("select kind, model, digest from embed_cache").fetchall()
    assert rows == [("sparse", "fastembed9.9/splade/prithivida/Splade_PP_en_v1",
                     hashlib.sha256(b"alpha beta").hexdigest())]


@pytest.mark.parametrize("payload", ["[[1], [1.0, 2.0]]", "[1.0]", "[]", '"x"'])
def test_sparse_cache_rejects_malformed_payloads_and_recomputes(monkeypatch, fake_models, cache_path, payload):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    first = embeddings.sparse_embed(["alpha beta"])
    with sqlite3.connect(cache_path) as db:
        db.execute("update embed_cache set payload = ?", (payload,))
    assert embeddings.sparse_embed(["alpha beta"]) == first
    assert len(fake_models["sparse"].calls) == 2


def test_sparse_embed_uses_the_cache_only_when_configured_and_enabled(monkeypatch, fake_models, cache_path):
    used = []
    monkeypatch.setattr(embeddings, "_cached", lambda *a, **kw: used.append(a) or [])
    monkeypatch.setattr(config, "SPARSE_BACKEND", "none")  # cache path set, sparse disabled
    with pytest.raises(ValueError) as e:
        embeddings.sparse_embed(["alpha"])
    assert _message(e) == "invalid sparse backend" and used == []
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    monkeypatch.setattr(config, "EMBED_CACHE_PATH", "")  # sparse enabled, no cache configured
    assert len(embeddings.sparse_embed(["alpha"])) == 1 and used == []


# ---- query-path wiring ----------------------------------------------------------------------------

def test_embed_query_returns_the_sparse_vector_only_when_hybrid(monkeypatch, fake_models):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    dense, sparse = retrieval._embed_query("alpha beta")
    assert len(dense) == 384 and sparse == embeddings.sparse_embed_one("alpha beta") and sparse[0]
    monkeypatch.setattr(config, "SPARSE_BACKEND", "none")
    assert retrieval._embed_query("alpha beta")[1] is None


def test_vector_hits_forwards_pool_and_sparse_to_search(monkeypatch):
    calls = []
    monkeypatch.setattr(retrieval, "search", lambda *a, **kw: calls.append((a, kw)) or ["hit"])
    monkeypatch.setattr(retrieval, "search_unfiltered_count", lambda *a, **kw: calls.append((a, kw)) or 5)
    sparse = ([1], [1.0])
    assert retrieval._vector_hits([0.1], ["*"], 3, sparse, 7) == (["hit"], 5)
    assert calls == [(([0.1], ["*"]), {"top_k": 7, "sparse": sparse}), (([0.1], ["*"]), {"top_k": 3})]
    calls.clear()
    retrieval._vector_hits([0.1], ["*"], 3)
    assert calls[0] == (([0.1], ["*"]), {"top_k": 3})  # no sparse kwarg on the dense-only path


def test_retrieve_hands_the_query_sparse_vector_to_the_search(hybrid_index, monkeypatch):
    seen = []
    real = retrieval.search
    monkeypatch.setattr(retrieval, "search", lambda *a, **kw: seen.append(kw) or real(*a, **kw))
    retrieval.clear_cache()
    out = retrieval.retrieve("vacation policy days", _principal("guest"))
    assert out["source"] == "full_rag" and out["results"]
    assert seen == [{"top_k": config.TOP_K, "sparse": embeddings.sparse_embed_one("vacation policy days")}]


def test_retrieve_sends_no_sparse_vector_on_a_dense_only_index(client, monkeypatch):
    seen = []
    real = retrieval.search
    monkeypatch.setattr(retrieval, "search", lambda *a, **kw: seen.append(kw) or real(*a, **kw))
    retrieval.clear_cache()
    retrieval.retrieve("vacation policy days", _principal("guest"))
    assert seen == [{"top_k": config.TOP_K}]


# ---- reranker wiring and degradation ---------------------------------------------------------------

def test_reranker_receives_the_real_query(reranked):
    retrieval.retrieve("vacation policy days", _principal("guest"), k=3)
    assert [q for q, _docs in reranked.seen] == ["vacation policy days"]


def test_reranker_outage_logs_the_exact_warning_and_reports_degraded_outcome(reranked, caplog):
    reranked.fn = lambda q, d: (_ for _ in ()).throw(RuntimeError("onnx exploded"))
    with caplog.at_level(logging.WARNING, logger="permrag"):
        out = retrieval.retrieve("vacation policy days", _principal("guest"), k=3)
    assert "reranker unavailable (RuntimeError); using vector order" in [r.getMessage() for r in caplog.records]
    assert out["source"] == "vector_order" and out["degraded"] is True
    assert retrieval._outcome(out) == "degraded"


@pytest.mark.parametrize("source,results,expected", [
    ("vector_order", [{"doc_id": "x"}], "degraded"),
    ("vector_order", [], "degraded"),
    ("keyword_fallback", [{"doc_id": "x"}], "degraded"),
    ("unavailable", [], "failed_closed"),
    ("retrieval_only", [{"doc_id": "x"}], "llm_fallback"),
    ("full_rag", [{"doc_id": "x"}], "ok"),
    ("full_rag", [], "no_results"),
])
def test_outcome_maps_every_source(source, results, expected):
    assert retrieval._outcome({"source": source, "results": results}) == expected
