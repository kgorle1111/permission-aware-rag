"""Qdrant with ACL payload filtering — the pre-filter enforced by the engine.

The ACL check is a payload predicate evaluated DURING HNSW traversal (or the
local-mode equivalent), not a post-hoc filter: forbidden points are never
scored, so nothing about them can influence results, ranks, or counts.

Local embedded mode by default (QDRANT_PATH); set QDRANT_URL for a server —
identical API either way.
"""
from __future__ import annotations

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
    client().upsert(config.COLLECTION, points=[
        PointStruct(id=ch["id"], vector=ch["vector"],
                    payload={"doc_id": ch["doc_id"], "text": ch["text"], "acl": ch["acl"]})
        for ch in chunks
    ])


def search(query_vector: list[float], principals: list[str], top_k: int) -> list[dict]:
    """THE security boundary: acl-match filter applied inside the engine."""
    hits = client().query_points(
        collection_name=config.COLLECTION,
        query=query_vector,
        query_filter=Filter(must=[FieldCondition(key="acl", match=MatchAny(any=principals))]),
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
        limit=top_k, score_threshold=config.MIN_SCORE, with_payload=["acl"],
    ).points
    pset = set(principals)
    return sum(1 for h in hits if not (set(h.payload.get("acl", [])) & pset))


def update_doc_acl(doc_id: str, acl: list[str]):
    """Permission sync writes flow through here (plus Postgres source of truth)."""
    client().set_payload(
        collection_name=config.COLLECTION,
        payload={"acl": acl},
        points=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
    )


def delete_doc(doc_id: str):
    client().delete(
        collection_name=config.COLLECTION,
        points_selector=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
    )
