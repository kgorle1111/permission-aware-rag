"""Embeddings as a security decision: they run LOCALLY. Confidential document
text never transits a third-party API to become a vector — a permission-aware
RAG system that ships plaintext to a hosted embedder has reintroduced the
exact exposure it claims to prevent.

Default backend is a hashing-trick bag-of-ngrams embedder: offline,
deterministic, zero heavy deps. The permission model's security properties do
not depend on embedding quality — retrieval quality does, so for production
set EMBED_BACKEND=st (sentence-transformers, BAAI/bge-small-en-v1.5, also
local). Same interface, one env var.
"""
from __future__ import annotations

import hashlib
import math
import re

from . import config

# ponytail: hashing-trick embedder — swap to sentence-transformers via
# EMBED_BACKEND=st when retrieval quality matters more than install weight.

_st_model = None

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


def embed(texts: list[str]) -> list[list[float]]:
    if config.EMBED_BACKEND not in {"hash", "st"} or config.EMBED_DIM < 1:
        raise ValueError("invalid embedding backend or dimension")
    if config.EMBED_BACKEND == "st":
        global _st_model
        if _st_model is None:
            from sentence_transformers import SentenceTransformer
            _st_model = SentenceTransformer("BAAI/bge-small-en-v1.5")
        return _st_model.encode(texts, normalize_embeddings=True).tolist()
    return [_hash_embed(t, config.EMBED_DIM) for t in texts]


def embed_one(text: str) -> list[float]:
    return embed([text])[0]
