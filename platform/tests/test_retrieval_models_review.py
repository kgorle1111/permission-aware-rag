"""Review fixes for PR F: dense floor in hybrid, cache pruning/versioning, floor default, empty rerank."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app import config, embeddings, ingest, resilience, retrieval, vectorstore
from conftest import CORPUS, reingest
from test_retrieval_models import (FakeDense, FakeReranker, FakeSparse, _principal,  # noqa: F401
                                   cache_path, fake_models, hybrid_index, reranked)


@pytest.fixture(autouse=True)
def _fresh_breakers():
    resilience.reset_breakers()
    yield
    resilience.reset_breakers()


def _raw_chunk(i, doc, dense_axis, sparse_idx):
    vec = [0.0] * 384
    vec[dense_axis] = 1.0
    return {"id": i, "doc_id": doc, "text": doc, "acl": ["*"], "acl_doc": ["*"], "acl_section": ["*"],
            "acl_para": ["*"], "vector": vec, "sparse": ([sparse_idx], [1.0])}


# ---- 1. hybrid honours the dense floor -------------------------------------------

def test_hybrid_never_returns_a_chunk_below_the_dense_floor(client, monkeypatch, fake_models):
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    q = [0.0] * 384
    q[0] = 1.0
    try:
        vectorstore.reset_collection()
        vectorstore.upsert_chunks([_raw_chunk(1, "qualified", 0, 5), _raw_chunk(2, "subfloor", 1, 7)])
        # the sub-floor chunk shares the query's only sparse term, but must not enter via the sparse leg
        assert [h["doc_id"] for h in vectorstore.search(q, ["*"], 5, sparse=([7], [1.0]))] == ["qualified"]
        assert [h["doc_id"] for h in vectorstore.search(q, ["*"], 5, sparse=([5], [1.0]))] == ["qualified"]
        orthogonal = [0.0] * 384
        orthogonal[2] = 1.0
        assert vectorstore.search(orthogonal, ["*"], 5, sparse=([7], [1.0])) == []
    finally:
        monkeypatch.undo()
        reingest()


def test_one_shared_term_plus_nonsense_matches_dense_only_behaviour(hybrid_index):
    query = "meals " + " ".join(f"zq{i}x" for i in range(200))
    guest = _principal("guest")
    dense_only = {h["text"] for h in vectorstore.search(embeddings.embed_one(query), guest.principals, 20)}
    out = retrieval.retrieve(query, guest, k=20)
    assert out["source"] == "full_rag"
    assert {r["text"] for r in out["results"]} <= dense_only  # sparse never adds a sub-floor chunk

# ---- 2. cache pruning ------------------------------------------------------------

def _digests(path):
    with sqlite3.connect(path) as db:
        return {d for (d,) in db.execute("select digest from embed_cache")}


def test_reingest_prunes_cache_rows_for_deleted_text(client, monkeypatch, fake_models, cache_path, tmp_path):
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    monkeypatch.setattr(config, "SPARSE_BACKEND", "splade")
    a = {"doc_id": "a", "acl": ["*"], "sections": [{"text": "alpha policy text"}]}
    b = {"doc_id": "b", "acl": ["*"], "sections": [{"text": "bravo secret memo"}]}
    both, only_a = tmp_path / "both.json", tmp_path / "a.json"
    both.write_text(json.dumps([a, b]))
    only_a.write_text(json.dumps([a]))
    sha = lambda t: hashlib.sha256(t.encode()).hexdigest()  # noqa: E731
    try:
        ingest.ingest_corpus(both)
        assert {sha("alpha policy text"), sha("bravo secret memo")} <= _digests(cache_path)
        ingest.ingest_corpus(only_a)
        assert _digests(cache_path) == {sha("alpha policy text")}
    finally:
        monkeypatch.undo()
        reingest()


def test_prune_without_a_cache_file_does_nothing(tmp_path, monkeypatch):
    path = tmp_path / "absent.sqlite"
    monkeypatch.setattr(config, "EMBED_CACHE_PATH", str(path))
    embeddings.prune_cache(["x"])
    assert not path.exists()
    monkeypatch.setattr(config, "EMBED_CACHE_PATH", "")
    embeddings.prune_cache(["x"])


# ---- 3. library version in the cache key -----------------------------------------

def test_cache_key_includes_fastembed_version(monkeypatch, fake_models, cache_path):
    monkeypatch.setattr(config, "EMBED_BACKEND", "fastembed")
    monkeypatch.setattr(embeddings.importlib.metadata, "version", lambda name: "1.2.3" if name == "fastembed" else "?")
    embeddings.embed(["alpha beta"])
    with sqlite3.connect(cache_path) as db:
        assert db.execute("select model from embed_cache").fetchall() == [
            ("fastembed1.2.3/BAAI/bge-small-en-v1.5/384",)]
    monkeypatch.setattr(embeddings.importlib.metadata, "version", lambda name: "1.2.4")
    embeddings.embed(["alpha beta"])
    assert len(fake_models["dense"].calls) == 2  # upgraded library never reuses old vectors


def test_missing_fastembed_distribution_keys_as_none(monkeypatch):
    def boom(name):
        raise embeddings.importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(embeddings.importlib.metadata, "version", boom)
    assert embeddings._fastembed_version() == "none"


# ---- 4. floor default follows the bge-based backends -----------------------------

@pytest.mark.parametrize("backend, expected", [("st", "0.55"), ("fastembed", "0.55"), ("hash", "0.1")])
def test_min_score_default_per_backend(backend, expected):
    env = {**os.environ, "EMBED_BACKEND": backend}
    env.pop("MIN_SCORE", None)
    out = subprocess.run([sys.executable, "-c", "from app import config; print(config.MIN_SCORE)"],
                         cwd=Path(config.ROOT), env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == expected


# ---- 5. no reranker call without candidates --------------------------------------

def test_reranker_is_not_called_when_there_are_no_hits(reranked):
    out = retrieval.retrieve("zorblax quantum unicorn farming", _principal("guest"))
    assert out["results"] == [] and out["degraded"] is False and out["source"] == "full_rag"
    assert reranked.seen == []
    assert resilience.BREAKERS["reranker"].state == "closed"
