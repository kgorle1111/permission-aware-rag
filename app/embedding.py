"""Deterministic feature-hash embeddings — stdlib only, no model, no network.

Each token hashes into one of DIMS buckets with a ±1 sign (feature hashing /
the hashing trick), and the vector is L2-normalized, so cosine similarity
approximates bag-of-words cosine. Good enough to demonstrate and test the
pgvector + RLS retrieval path end to end.

kn: placeholder embedder — swap embed() for Voyage AI or sentence-transformers
when semantic recall matters; the RLS/ACL logic is unchanged by that swap.
"""

import hashlib
import math

from permission_rag import tokenize

DIMS = 256


def embed(text: str) -> list[float]:
    v = [0.0] * DIMS
    for t in tokenize(text):
        h = int.from_bytes(hashlib.md5(t.encode()).digest()[:8], "big")
        v[h % DIMS] += 1.0 if h & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def to_pgvector(v: list[float]) -> str:
    """Literal for a ::vector cast — avoids needing the pgvector python adapter."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
