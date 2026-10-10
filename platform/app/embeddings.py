"""Embeddings as a security decision: they run LOCALLY. Confidential document
text never transits a third-party API to become a vector — a permission-aware
RAG system that ships plaintext to a hosted embedder has reintroduced the
exact exposure it claims to prevent.

Default backend is a hashing-trick bag-of-ngrams embedder: offline,
deterministic, zero heavy deps. The permission model's security properties do
not depend on embedding quality — retrieval quality does, so for production
set EMBED_BACKEND=fastembed (ONNX BAAI/bge-small-en-v1.5, also local, no
PyTorch; "st" is the sentence-transformers equivalent). Same interface, one env var.

Learned-sparse vectors (SPARSE_BACKEND=splade) are computed per document or
query from the text alone. Nothing here may use collection-wide statistics:
IDF-style weighting would be a ranking statistic over rows the caller cannot read.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import re
import sqlite3
import threading

from . import config

# ponytail: hashing-trick embedder — swap to fastembed via
# EMBED_BACKEND=fastembed when retrieval quality matters more than install weight.

_st_model = None
_fe_lock = threading.Lock()
_fe_models: dict = {}

_STOP = frozenset(
    "the a an is are was were what when where who how do does did i we you "
    "our your of to in on for with and or per up it this that at by from as "
    "be can get my s".split())


def _hash_embed(text: str, dim: int) -> list[float]:
    # 3 hash functions per gram (Bloom-style): a true shared gram aligns all
    # three buckets; a random collision aligns ~one — keeps the noise floor
    # well below MIN_SCORE so "no match" is decisively no match.
    vec = [0.0] * dim
    raw = re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()
    words = [w for w in raw if w not in _STOP]  # stopwords carry no meaning, only noise
    grams = words + [" ".join(p) for p in zip(words, words[1:])]
    for g in grams:
        for seed in (b"h1:", b"h2:", b"h3:"):
            h = int.from_bytes(hashlib.md5(seed + g.encode()).digest()[:8], "big")
            vec[h % dim] += 1.0 if (h >> 63) else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def model_name() -> str:
    default = "BAAI/bge-small-en-v1.5" if config.EMBED_BACKEND in {"st", "fastembed"} else "hash-ngram-v1"
    return config.EMBED_MODEL or default


def sparse_enabled() -> bool:
    if config.SPARSE_BACKEND not in {"none", "splade"}:
        raise ValueError("invalid sparse backend")
    return config.SPARSE_BACKEND == "splade"


def sparse_model_name() -> str:
    # SPLADE++ weights are learned and per-text. BM42 was rejected: its scores
    # are only meaningful with Qdrant's IDF modifier, which uses collection-wide
    # document frequencies (the S1 side channel).
    return config.SPARSE_MODEL or "prithivida/Splade_PP_en_v1"


def fingerprint() -> tuple[str, str, int]:
    """Identity of the embedding space; vectors from different spaces are not comparable.

    The sparse model is folded into the model string so switching SPARSE_BACKEND
    without reingesting fails closed, with no schema change.
    """
    model = model_name()
    if sparse_enabled():
        model += f"+sparse:{sparse_model_name()}"
    return config.EMBED_BACKEND, model, config.EMBED_DIM


def _load_fastembed(kind: str, name: str):
    """One process-wide ONNX session per (kind, model); the only place a model is fetched."""
    with _fe_lock:
        model = _fe_models.get((kind, name))
        if model is None:
            kwargs = {"cache_dir": config.MODEL_CACHE_DIR} if config.MODEL_CACHE_DIR else {}
            if kind == "dense":
                from fastembed import TextEmbedding
                model = TextEmbedding(name, **kwargs)
            elif kind == "sparse":
                from fastembed import SparseTextEmbedding
                model = SparseTextEmbedding(name, **kwargs)
            elif kind == "rerank":
                from fastembed.rerank.cross_encoder import TextCrossEncoder
                model = TextCrossEncoder(name, **kwargs)
            else:
                raise ValueError("unknown model kind")
            _fe_models[(kind, name)] = model
        return model


def _encode(texts: list[str]) -> list[list[float]]:
    if config.EMBED_BACKEND not in {"hash", "st", "fastembed"} or config.EMBED_DIM < 1:
        raise ValueError("invalid embedding backend or dimension")
    if config.EMBED_BACKEND == "st":
        global _st_model
        if _st_model is None:
            from sentence_transformers import SentenceTransformer
            _st_model = SentenceTransformer(model_name())
        return _st_model.encode(texts, normalize_embeddings=True).tolist()
    if config.EMBED_BACKEND == "fastembed":
        vectors = [[float(x) for x in v] for v in _load_fastembed("dense", model_name()).embed(texts)]
        if any(len(v) != config.EMBED_DIM for v in vectors):
            raise ValueError("embedding dimension mismatch")
        return vectors
    return [_hash_embed(t, config.EMBED_DIM) for t in texts]


def _sparse_encode(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    if not sparse_enabled():
        raise ValueError("invalid sparse backend")
    return [([int(i) for i in e.indices], [float(v) for v in e.values])
            for e in _load_fastembed("sparse", sparse_model_name()).embed(texts)]


def _cached(kind: str, model_id: str, texts: list[str], compute, valid, decode):
    """Reingest must not re-embed unchanged text. Keyed sha256(text) + model id.

    Document path only: a cache shared with queries would let one user's
    query speed up another's, a cross-principal timing signal. Vectors are a
    pure function of the (already redacted) text, so no ACL data enters the cache.
    """
    digests = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
    with closing(sqlite3.connect(config.EMBED_CACHE_PATH)) as db:
        db.execute("create table if not exists embed_cache (kind text, model text, digest text, "
                   "payload text, primary key (kind, model, digest))")
        found = {}
        for digest in dict.fromkeys(digests):
            row = db.execute("select payload from embed_cache where kind=? and model=? and digest=?",
                             (kind, model_id, digest)).fetchone()
            if row is not None and valid(value := json.loads(row[0])):
                found[digest] = decode(value)
        todo = {d: t for d, t in zip(digests, texts) if d not in found}
        if todo:
            for digest, value in zip(todo, compute(list(todo.values()))):
                db.execute("insert or replace into embed_cache values (?, ?, ?, ?)",
                           (kind, model_id, digest, json.dumps(value)))
                found[digest] = decode(value)
            db.commit()
    return [found[d] for d in digests]


def embed(texts: list[str]) -> list[list[float]]:
    """Document path (ingest); fastembed vectors are cached across reingests."""
    if config.EMBED_BACKEND != "fastembed" or not config.EMBED_CACHE_PATH:
        return _encode(texts)
    return _cached("dense", f"fastembed/{model_name()}/{config.EMBED_DIM}", texts, _encode,
                   lambda v: isinstance(v, list) and len(v) == config.EMBED_DIM, list)


def embed_one(text: str) -> list[float]:
    """Query path: never cached."""
    return _encode([text])[0]


def sparse_embed(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    """Document path for learned-sparse vectors; cached like dense."""
    if not config.EMBED_CACHE_PATH or not sparse_enabled():
        return _sparse_encode(texts)
    return _cached("sparse", f"splade/{sparse_model_name()}", texts, _sparse_encode,
                   lambda v: isinstance(v, list) and len(v) == 2 and len(v[0]) == len(v[1]),
                   lambda v: (list(v[0]), list(v[1])))


def sparse_embed_one(text: str) -> tuple[list[int], list[float]]:
    return _sparse_encode([text])[0]
