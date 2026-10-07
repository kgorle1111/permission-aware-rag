"""Qdrant with ACL payload filtering — the pre-filter enforced by the engine.

The result query carries an ACL predicate to the engine; only matching
points are returned to generation. Engine scoring internals vary by backend.
The separate audit query samples unfiltered top-k with ACL-only payloads.

Local embedded mode by default (QDRANT_PATH); set QDRANT_URL for a server —
identical API either way.
"""
from __future__ import annotations

import atexit

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

from . import config

ACL_LEVELS = ("acl_doc", "acl_section", "acl_para")

_client: QdrantClient | None = None


def client() -> QdrantClient:
    global _client
    if _client is None:
        if config.QDRANT_URL:
            _client = QdrantClient(url=config.QDRANT_URL)
        else:
            _client = QdrantClient(path=config.QDRANT_PATH)
    return _client


def reset_collection():
    c = client()
    if c.collection_exists(config.COLLECTION):
        c.delete_collection(config.COLLECTION)
    c.create_collection(
        collection_name=config.COLLECTION,
        vectors_config=VectorParams(size=config.EMBED_DIM, distance=Distance.COSINE),
    )


def upsert_chunks(chunks: list[dict]):
    """chunks: [{id:int, doc_id, text, acl:[...], vector:[...]}]"""
    client().upsert(config.COLLECTION, wait=True, points=[
        PointStruct(id=ch["id"], vector=ch["vector"],
                    payload={"doc_id": ch["doc_id"], "text": ch["text"], "acl": ch["acl"], **{level: ch[level] for level in ACL_LEVELS}})
        for ch in chunks
    ])


def search(query_vector: list[float], principals: list[str], top_k: int) -> list[dict]:
    """THE security boundary: acl-match filter applied inside the engine."""
    hits = client().query_points(
        collection_name=config.COLLECTION,
        query=query_vector,
        query_filter=Filter(must=[FieldCondition(key=level, match=MatchAny(any=principals)) for level in ACL_LEVELS]),
        limit=top_k,
        score_threshold=config.MIN_SCORE,
        with_payload=True,
    ).points
    return [{"chunk_id": h.id, "score": h.score, **h.payload} for h in hits]


def search_unfiltered_count(query_vector: list[float], principals: list[str], top_k: int) -> int:
    """How many of the top-k relevant chunks were hidden from this user —
    the audit signal ('denied_count'). Internal only: results never leave
    this function, only the count does."""
    hits = client().query_points(
        collection_name=config.COLLECTION, query=query_vector,
        limit=top_k, score_threshold=config.MIN_SCORE, with_payload=list(ACL_LEVELS),
    ).points
    pset = set(principals)
    return sum(1 for h in hits if not all(set(h.payload.get(level, [])) & pset for level in ACL_LEVELS))


def delete_doc(doc_id: str):
    client().delete(
        collection_name=config.COLLECTION,
        wait=True,
        points_selector=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
    )


def update_chunk_acl(chunk_id: int, acl: list[str], *, levels: dict | None = None):
    if not client().retrieve(config.COLLECTION, ids=[chunk_id],
                             with_payload=False, with_vectors=False):
        raise RuntimeError("indexed chunk missing; full reingestion required")
    client().set_payload(collection_name=config.COLLECTION,
                         payload={"acl": acl, **(levels or {"acl_doc": acl})}, points=[chunk_id], wait=True)


def close_client():
    global _client
    if _client is not None:
        _client.close()
        _client = None


atexit.register(close_client)
