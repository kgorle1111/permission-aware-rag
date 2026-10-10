"""PR F: real embeddings, learned-sparse hybrid, local reranker, async path.

No test downloads a model: every model call is a deterministic fake behind
embeddings._load_fastembed. Real-model numbers live in scripts/rung_benchmark.py.
"""
import asyncio
import json
import logging
import sqlite3
import zlib
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from qdrant_client.models import Filter, SparseVector

from app import config, embeddings, ingest, rerank, resilience, retrieval, vectorstore
from app.identity import Principal
from conftest import CORPUS, USERS, forbidden_canaries, reingest


def _principal(name):
    sub, groups, _ = USERS[name]
    return Principal(sub, tuple(groups))


@pytest.fixture(autouse=True)
def _fresh_breakers():
    resilience.reset_breakers()
    yield
    resilience.reset_breakers()


# ---- deterministic fakes (no network, no ONNX) -----------------------------------

class FakeDense:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        for t in texts:
            yield np.array(embeddings._hash_embed(t, 384), dtype=np.float32)


class FakeSparse:
    """Word-hash sparse vectors: a stand-in for SPLADE with per-document weights only."""

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        for t in texts:
            words = sorted({w for w in t.lower().replace(".", " ").replace(",", " ").split() if len(w) > 2})
            yield SimpleNamespace(
                indices=np.array([zlib.crc32(w.encode()) % 30000 for w in words], dtype=np.int64),
                values=np.array([1.0] * len(words), dtype=np.float32))


class FakeReranker:
    def __init__(self, fn=None):
        self.seen = []
        self.fn = fn or (lambda q, d: float(len(set(q.lower().split()) & set(d.lower().split()))))

    def rerank(self, query, docs):
        self.seen.append((query, list(docs)))
        return [self.fn(query, d) for d in docs]


@pytest.fixture()
def fake_models(monkeypatch):
    models = {"dense": FakeDense(), "sparse": FakeSparse(), "rerank": FakeReranker()}
    monkeypatch.setattr(embeddings, "_load_fastembed", lambda kind, name: models[kind])
    return models


@pytest.fixture()
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "embed_cache.sqlite"
    monkeypatch.setattr(config, "EMBED_CACHE_PATH", str(path))
    return path


@pytest.fixture()
def hybrid_index(client, monkeypatch, fake_models):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    ingest.ingest_corpus(CORPUS)
    yield fake_models
    monkeypatch.undo()
    reingest()


# ---- 9.1 fastembed dense backend + fingerprint -----------------------------------

def test_fastembed_backend_embeds_with_default_bge_model_and_checks_dim(monkeypatch, fake_models):
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    monkeypatch.setattr(config, "EMBED_CACHE_PATH", "")
    asked = []
    monkeypatch.setattr(embeddings, "_load_fastembed", lambda kind, name: asked.append((kind, name)) or fake_models["dense"])
    out = embeddings.embed(["alpha beta"])
    assert asked == [("dense", "BAAI/bge-small-en-v1.5")]
    assert len(out) == 1 and len(out[0]) == 384 and all(isinstance(v, float) for v in out[0])
    monkeypatch.setattr(config, "EMBED_DIM", 8)
    with pytest.raises(ValueError, match="embedding dimension mismatch"):
        embeddings.embed(["alpha beta"])


def test_fingerprint_names_backend_model_dim_and_sparse_model(monkeypatch):
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    assert embeddings.fingerprint() == ("fastembed", "BAAI/bge-small-en-v1.5", 384)
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    assert embeddings.fingerprint() == (
        "fastembed", "BAAI/bge-small-en-v1.5+sparse:prithivida/Splade_PP_en_v1", 384)


def test_unknown_sparse_backend_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "bm42")
    with pytest.raises(ValueError, match="invalid sparse backend"):
        embeddings.fingerprint()
    with pytest.raises(ValueError, match="invalid sparse backend"):
        embeddings.sparse_embed(["x"])


def test_sparse_embedding_is_refused_when_the_backend_is_disabled(monkeypatch):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "none")
    assert embeddings.sparse_enabled() is False
    with pytest.raises(ValueError, match="invalid sparse backend"):
        embeddings.sparse_embed_one("x")


def test_switching_sparse_backend_without_reingest_fails_closed(client, monkeypatch, caplog):
    embed = Mock()
    monkeypatch.setattr(retrieval, "embed_one", embed)
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    with caplog.at_level(logging.ERROR, logger="permrag"):
        assert retrieval.retrieve("vacation policy", _principal("alice")) == retrieval.EMPTY_RESPONSE
    embed.assert_not_called()
    assert "index built with hash/hash-ngram-v1/384; configured hash/hash-ngram-v1+sparse:" in caplog.text
    assert client.get("/readyz").status_code == 503


def test_fastembed_index_fingerprint_is_persisted_and_mismatch_with_hash_fails_closed(
        client, monkeypatch, fake_models, cache_path):
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    try:
        ingest.ingest_corpus(CORPUS)
        with retrieval.permission_transaction() as (s, _state):
            assert retrieval.fingerprint_problem(s) is None
        out = retrieval.retrieve("vacation policy days", _principal("alice"))
        assert out["source"] == "full_rag" and out["results"]
        monkeypatch.setattr(config, "EMBED_BACKEND", "hash")
        retrieval.clear_cache()
        assert retrieval.retrieve("vacation policy days", _principal("alice")) == retrieval.EMPTY_RESPONSE
    finally:
        monkeypatch.undo()
        reingest()


# ---- 8.6 embedding cache ---------------------------------------------------------

def test_cache_serves_unchanged_text_and_embeds_only_new_text(monkeypatch, fake_models, cache_path):
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    first = embeddings.embed(["alpha beta", "gamma delta"])
    assert fake_models["dense"].calls == [["alpha beta", "gamma delta"]]
    second = embeddings.embed(["gamma delta", "epsilon zeta", "alpha beta"])
    assert fake_models["dense"].calls[1:] == [["epsilon zeta"]]
    assert second[0] == first[1] and second[2] == first[0]
    embeddings.embed(["alpha beta"])
    assert len(fake_models["dense"].calls) == 2  # fully cached batch makes no model call


def test_cache_key_is_sha256_of_text_plus_model_identity(monkeypatch, fake_models, cache_path):
    import hashlib
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    embeddings.embed(["alpha beta"])
    with sqlite3.connect(cache_path) as db:
        rows = db.execute("select kind, model, digest from embed_cache").fetchall()
    assert rows == [("dense", "fastembed/BAAI/bge-small-en-v1.5/384",
                     hashlib.sha256(b"alpha beta").hexdigest())]
    monkeypatch.setattr(config, "EMBED_MODEL", "other/model")
    embeddings.embed(["alpha beta"])
    assert len(fake_models["dense"].calls) == 2  # different model id never reuses a vector


def test_cache_ignores_wrong_length_entries(monkeypatch, fake_models, cache_path):
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    embeddings.embed(["alpha beta"])
    with sqlite3.connect(cache_path) as db:
        db.execute("update embed_cache set payload = '[1.0, 2.0]'")
    assert len(embeddings.embed(["alpha beta"])[0]) == 384
    assert len(fake_models["dense"].calls) == 2


def test_query_path_never_reads_or_writes_the_cache(monkeypatch, fake_models, cache_path):
    # A shared cache on the query path would let one user's query speed up
    # another's: a cross-principal timing signal. Documents only.
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    embeddings.embed(["alpha beta"])
    embeddings.embed_one("alpha beta")
    embeddings.sparse_embed(["alpha beta"])
    embeddings.sparse_embed_one("alpha beta")
    assert fake_models["dense"].calls == [["alpha beta"], ["alpha beta"]]
    assert fake_models["sparse"].calls == [["alpha beta"], ["alpha beta"]]
    with sqlite3.connect(cache_path) as db:
        assert db.execute("select count(*) from embed_cache").fetchone() == (2,)  # one dense, one sparse


def test_hash_backend_never_creates_a_cache_file(tmp_path, monkeypatch):
    path = tmp_path / "must-not-exist.sqlite"
    monkeypatch.setattr(config, "EMBED_CACHE_PATH", str(path))
    embeddings.embed(["alpha beta"])
    assert not path.exists()


def test_reingest_does_not_reembed_unchanged_chunks(client, monkeypatch, fake_models, cache_path):
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    try:
        ingest.ingest_corpus(CORPUS)
        embedded = sum(len(c) for c in fake_models["dense"].calls)
        assert embedded > 0
        ingest.ingest_corpus(CORPUS)
        assert sum(len(c) for c in fake_models["dense"].calls) == embedded
    finally:
        monkeypatch.undo()
        reingest()


# ---- 9.5 learned-sparse hybrid ---------------------------------------------------

def test_sparse_collection_config_has_no_idf_modifier(hybrid_index):
    # Qdrant's IDF modifier computes collection-wide document frequencies, a
    # ranking statistic over rows the caller cannot read (AGENTS.md invariant 2, S1).
    info = vectorstore.client().get_collection(config.COLLECTION)
    sparse = info.config.params.sparse_vectors
    assert set(sparse) == {"sparse"}
    assert sparse["sparse"].modifier is None
    assert set(info.config.params.vectors) == {"dense"}


def test_default_collection_stays_dense_only(client):
    info = vectorstore.client().get_collection(config.COLLECTION)
    assert not info.config.params.sparse_vectors
    assert not isinstance(info.config.params.vectors, dict)


def _acl_filter_keys(flt):
    assert isinstance(flt, Filter)
    return sorted((c.key, tuple(c.match.any)) for c in flt.must)


def test_hybrid_query_applies_acl_must_filter_to_both_prefetches_and_the_outer_query(hybrid_index, monkeypatch):
    real = vectorstore.client()
    seen = []
    original = real.query_points
    monkeypatch.setattr(real, "query_points", lambda **kw: seen.append(kw) or original(**kw))
    principals = ["*", "user:guest@external.com"]
    vectorstore.search([0.1] * 384, principals, 4, sparse=([1, 2], [1.0, 1.0]))
    [kw] = seen
    expected = sorted((lvl, tuple(principals)) for lvl in vectorstore.ACL_LEVELS)
    assert [p.using for p in kw["prefetch"]] == ["dense", "sparse"]
    for branch in kw["prefetch"]:
        assert _acl_filter_keys(branch.filter) == expected
    assert _acl_filter_keys(kw["query_filter"]) == expected
    assert kw["limit"] == 4 and all(p.limit == 8 for p in kw["prefetch"])
    assert isinstance(kw["prefetch"][1].query, SparseVector)
    assert kw["prefetch"][0].score_threshold == config.MIN_SCORE


def test_hybrid_never_returns_a_hidden_document_even_for_its_exact_text(hybrid_index):
    for name in ("guest", "alice", "bob", "carol"):
        p = _principal(name)
        for doc_id, text in {
            "hr-salaries": "salary bands range from 90k to 250k HR-CANARY-2b8c1",
            "exec-comp": "CEO total package equity refresh EXEC-CANARY-9d4e2",
            "board-minutes": "unannounced acquisition of Northwind Labs BOARD-CANARY-5a6f0",
            "eng-oncall": "production database credentials vault ENG-CANARY-7f3a9",
        }.items():
            out = retrieval.retrieve(text, p, k=20)
            blob = str(out)
            for canary in forbidden_canaries(name):
                assert canary not in blob, (name, doc_id, canary)
            if doc_id not in USERS[name][2]:
                assert doc_id not in [r["doc_id"] for r in out["results"]]
            retrieval.clear_cache()


def test_authorized_user_finds_the_document_through_hybrid(hybrid_index):
    out = retrieval.retrieve("salary bands range from 90k to 250k", _principal("bob"))
    assert out["source"] == "full_rag"
    assert "hr-salaries" in [r["doc_id"] for r in out["results"]]


def _vec(text):
    return embeddings._hash_embed(text, 384)


def _sparse(text):
    return embeddings.sparse_embed_one(text)


def _chunk(i, doc, text, acl):
    return {"id": i, "doc_id": doc, "text": text, "acl": acl, "acl_doc": acl, "acl_section": ["*"],
            "acl_para": ["*"], "vector": _vec(text), "sparse": _sparse(text)}


def test_hybrid_score_of_a_visible_chunk_is_independent_of_hidden_chunks(client, monkeypatch, fake_models):
    """Adding hidden chunks that match the query better must not move a visible
    chunk's fused score (RRF ranks only filtered lists). Scores are compared exactly."""
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    query = "quarterly revenue forecast margin assumptions"
    visible = [
        _chunk(1, "v1", "quarterly revenue forecast for the sales team", ["*"]),
        _chunk(2, "v2", "margin assumptions are reviewed every month", ["*"]),
        _chunk(3, "v3", "office plants are watered on tuesdays", ["*"]),
    ]
    hidden = [
        _chunk(10, "h1", "quarterly revenue forecast margin assumptions exact secret", ["group:secret"]),
        _chunk(11, "h2", "quarterly revenue forecast margin assumptions", ["group:secret"]),
    ]
    q, sq = _vec(query), _sparse(query)
    try:
        vectorstore.reset_collection()
        vectorstore.upsert_chunks(visible)
        before = vectorstore.search(q, ["*"], 10, sparse=sq)
        vectorstore.reset_collection()
        vectorstore.upsert_chunks(visible + hidden)
        after = vectorstore.search(q, ["*"], 10, sparse=sq)
    finally:
        monkeypatch.undo()
        reingest()
    assert [(h["doc_id"], h["score"]) for h in before] == [(h["doc_id"], h["score"]) for h in after]
    assert {h["doc_id"] for h in after} <= {"v1", "v2", "v3"}
    assert len(before) >= 2


def test_hybrid_upsert_requires_a_sparse_vector(client, monkeypatch, fake_models):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    chunk = _chunk(1, "d", "text", ["*"])
    del chunk["sparse"]
    try:
        vectorstore.reset_collection()
        with pytest.raises(KeyError):
            vectorstore.upsert_chunks([chunk])
    finally:
        monkeypatch.undo()
        reingest()


def test_hybrid_denied_count_still_reports_hidden_neighbours(hybrid_index):
    from app.store import AuditLog, SessionLocal
    retrieval.retrieve("salary bands range from 90k to 250k", _principal("guest"))
    with SessionLocal() as s:
        row = s.query(AuditLog).filter(AuditLog.user_id == "guest@external.com").order_by(AuditLog.id.desc()).first()
    assert row.denied_count > 0 and row.returned_doc_ids == []


# ---- 9.6 local cross-encoder reranker --------------------------------------------

@pytest.fixture()
def reranked(client, monkeypatch, fake_models):
    monkeypatch.setattr(config, "RERANKER", "fastembed")
    monkeypatch.setattr(config, "RERANK_CANDIDATES", 25)
    monkeypatch.setattr(config, "RERANK_MIN_SCORE", 0.0)
    retrieval.clear_cache()
    yield fake_models["rerank"]
    retrieval.clear_cache()


def _readable_texts(principal):
    """Independent oracle: chunk visible iff the caller passes every ACL level."""
    granted = set(principal.principals)
    chunks = [c for d in json.loads(CORPUS.read_text()) for c in ingest.chunk_document(d)]
    return {c["text"] for c in chunks
            if all(granted & set(c[lvl]) for lvl in ("acl_doc", "acl_section", "acl_para"))}


def test_reranker_only_receives_acl_filtered_candidates(reranked):
    for name in ("guest", "alice", "bob"):
        reranked.seen.clear()
        retrieval.clear_cache()
        p = _principal(name)
        retrieval.retrieve("compensation salary credentials acquisition vacation", p, k=4)
        sent = {d for _q, docs in reranked.seen for d in docs}
        assert sent, f"reranker saw no candidates for {name}"
        assert sent <= _readable_texts(p), (name, sent - _readable_texts(p))
        for canary in forbidden_canaries(name):
            assert all(canary not in d for d in sent)


def test_reranker_reorders_thresholds_and_truncates_to_k(reranked):
    reranked.fn = lambda q, d: 7.0 if "Remote work" in d else 5.0 if "Vacation policy" in d else -1.0
    q = "vacation policy remote work expense policy"
    out = retrieval.retrieve(q, _principal("guest"), k=4)
    assert [r["score"] for r in out["results"]] == [7.0, 5.0]
    assert "Remote work" in out["results"][0]["text"]
    retrieval.clear_cache()
    assert [r["score"] for r in retrieval.retrieve(q, _principal("guest"), k=1)["results"]] == [7.0]


def test_reranker_candidate_pool_is_at_least_k_and_uses_configured_size(reranked, monkeypatch):
    seen = []
    real = retrieval.search
    monkeypatch.setattr(retrieval, "search", lambda *a, **kw: seen.append(kw["top_k"]) or real(*a, **kw))
    retrieval.retrieve("vacation policy", _principal("guest"), k=3)
    assert seen == [25]
    monkeypatch.setattr(config, "RERANK_CANDIDATES", 2)
    retrieval.clear_cache()
    retrieval.retrieve("vacation policy", _principal("guest"), k=3)
    assert seen[-1] == 3


def test_reranker_failure_falls_back_to_vector_order_and_marks_degraded(reranked, caplog):
    reranked.fn = lambda q, d: (_ for _ in ()).throw(RuntimeError("onnx exploded"))
    with caplog.at_level(logging.WARNING, logger="permrag"):
        out = retrieval.retrieve("vacation policy days", _principal("guest"), k=3)
    assert out["degraded"] is True and out["source"] == "vector_order"
    assert 1 <= len(out["results"]) <= 3
    assert "reranker unavailable (RuntimeError)" in caplog.text
    scores = [r["score"] for r in out["results"]]
    assert scores == sorted(scores, reverse=True)
    for canary in forbidden_canaries("guest"):
        assert canary not in str(out)


def test_reranker_failure_opens_its_own_breaker_and_is_not_cached(reranked):
    reranked.fn = lambda q, d: (_ for _ in ()).throw(RuntimeError("down"))
    for i in range(config.BREAKER_THRESHOLD):
        retrieval.retrieve(f"vacation policy {i}", _principal("guest"))
    assert resilience.BREAKERS["reranker"].state == "open"
    assert resilience.BREAKERS["embedder"].state == "closed"
    out = retrieval.retrieve("vacation policy again", _principal("guest"))
    assert out["degraded"] is True and out["source"] == "vector_order"


def test_rerank_unit_orders_by_score_with_chunk_id_tiebreak_and_inclusive_threshold(monkeypatch, fake_models):
    monkeypatch.setattr(config, "RERANKER", "fastembed")
    monkeypatch.setattr(config, "RERANK_MIN_SCORE", 1.0)
    scores = {"a": 1.0, "b": 3.0, "c": 1.0, "d": 0.99}
    fake_models["rerank"].fn = lambda q, d: scores[d]
    hits = [{"chunk_id": cid, "doc_id": "x", "text": t, "score": 0.5}
            for cid, t in ((9, "a"), (4, "c"), (5, "b"), (1, "d"))]
    out = rerank.rerank("q", hits, 10)
    assert [(h["text"], h["score"]) for h in out] == [("b", 3.0), ("c", 1.0), ("a", 1.0)]
    assert [h["text"] for h in rerank.rerank("q", hits, 2)] == ["b", "c"]
    assert hits[0]["score"] == 0.5  # inputs are not mutated


def test_rerank_unit_rejects_wrong_score_count_and_unknown_backend(monkeypatch, fake_models):
    monkeypatch.setattr(config, "RERANKER", "fastembed")
    fake_models["rerank"].rerank = lambda q, docs: [1.0]
    hits = [{"chunk_id": i, "doc_id": "x", "text": "t", "score": 0.5} for i in (1, 2)]
    with pytest.raises(ValueError, match="reranker returned 1 scores for 2 candidates"):
        rerank.rerank("q", hits, 5)
    monkeypatch.setattr(config, "RERANKER", "cohere")
    with pytest.raises(ValueError, match="invalid reranker backend"):
        rerank.rerank("q", hits, 5)


def test_rerank_unit_loads_configured_model(monkeypatch, fake_models):
    asked = []
    monkeypatch.setattr(config, "RERANKER", "fastembed")
    monkeypatch.setattr(embeddings, "_load_fastembed", lambda kind, name: asked.append((kind, name)) or fake_models["rerank"])
    rerank.rerank("q", [{"chunk_id": 1, "doc_id": "x", "text": "q", "score": 0.1}], 1)
    assert asked == [("rerank", "Xenova/ms-marco-MiniLM-L-6-v2")]


def test_load_fastembed_caches_models_and_passes_cache_dir(monkeypatch):
    built = []

    class T:
        def __init__(self, name, **kw):
            built.append((type(self).__name__, name, kw))

    fake = SimpleNamespace(TextEmbedding=type("TextEmbedding", (T,), {}),
                           SparseTextEmbedding=type("SparseTextEmbedding", (T,), {}))
    ce = SimpleNamespace(TextCrossEncoder=type("TextCrossEncoder", (T,), {}))
    monkeypatch.setitem(__import__("sys").modules, "fastembed", fake)
    monkeypatch.setitem(__import__("sys").modules, "fastembed.rerank.cross_encoder", ce)
    monkeypatch.setattr(embeddings, "_fe_models", {})
    monkeypatch.setattr(config, "MODEL_CACHE_DIR", "/models")
    a = embeddings._load_fastembed("dense", "m1")
    assert embeddings._load_fastembed("dense", "m1") is a
    embeddings._load_fastembed("sparse", "m2")
    embeddings._load_fastembed("rerank", "m3")
    monkeypatch.setattr(config, "MODEL_CACHE_DIR", "")
    embeddings._load_fastembed("dense", "m4")
    assert built == [("TextEmbedding", "m1", {"cache_dir": "/models"}),
                     ("SparseTextEmbedding", "m2", {"cache_dir": "/models"}),
                     ("TextCrossEncoder", "m3", {"cache_dir": "/models"}),
                     ("TextEmbedding", "m4", {})]
    with pytest.raises(ValueError, match="unknown model kind"):
        embeddings._load_fastembed("other", "m")


def test_sparse_embed_returns_index_value_lists(monkeypatch, fake_models):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    monkeypatch.setattr(config, "EMBED_CACHE_PATH", "")
    [(idx, val)] = embeddings.sparse_embed(["alpha beta"])
    assert set(idx) == {zlib.crc32(w.encode()) % 30000 for w in ("alpha", "beta")}
    assert val == [1.0, 1.0] and all(isinstance(i, int) for i in idx)


def test_sparse_documents_use_the_cache(monkeypatch, fake_models, cache_path):
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    a = embeddings.sparse_embed(["alpha beta"])
    b = embeddings.sparse_embed(["alpha beta"])
    assert a == b and len(fake_models["sparse"].calls) == 1


# ---- 8.4 async path --------------------------------------------------------------

def test_async_retrieval_with_interleaved_principals_has_no_cross_request_bleed(client):
    names = list(USERS)
    probes = {
        "alice": "production database credentials vault",
        "bob": "salary bands range from 90k to 250k",
        "carol": "revenue target gross margin assumptions",
        "ceo": "CEO total package equity refresh",
        "guest": "how many vacation days do we get",
    }
    work = [(names[i % len(names)], probes[names[(i * 3 + 1) % len(names)]]) for i in range(20)]
    sequential = {}
    for name, query in work:
        retrieval.clear_cache()
        r = retrieval.retrieve(query, _principal(name))
        sequential[(name, query)] = [(x["doc_id"], x["text"]) for x in r["results"]]

    async def run():
        retrieval.clear_cache()
        return await asyncio.gather(*(retrieval.aretrieve(q, _principal(n)) for n, q in work))

    out = asyncio.run(run())
    assert len(out) == 20
    for (name, query), response in zip(work, out):
        got = [(x["doc_id"], x["text"]) for x in response["results"]]
        assert got == sequential[(name, query)], (name, query)
        for canary in forbidden_canaries(name):
            assert canary not in str(response), (name, canary)


def test_aretrieve_passes_principal_and_k_explicitly(monkeypatch):
    calls = []
    monkeypatch.setattr(retrieval, "retrieve", lambda q, p, k=None: calls.append((q, p, k)) or {"ok": 1})
    p = Principal("a@b.c", ("g",))
    assert asyncio.run(retrieval.aretrieve("q", p, k=7)) == {"ok": 1}
    assert calls == [("q", p, 7)]
