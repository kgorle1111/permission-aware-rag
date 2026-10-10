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
    Fusion,
    FusionQuery,
    MatchAny,
    MatchValue,
    PointStruct,
    PayloadSchemaType,
    Prefetch,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from . import config
from .embeddings import sparse_enabled

ACL_LEVELS = ("acl_doc", "acl_section", "acl_para")

# Hybrid collections name their vectors; dense-only collections keep the unnamed default.
DENSE, SPARSE = "dense", "sparse"

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
    dense = VectorParams(size=config.EMBED_DIM, distance=Distance.COSINE, on_disk=True)
    hybrid = sparse_enabled()
    c.create_collection(
        collection_name=config.COLLECTION,
        vectors_config={DENSE: dense} if hybrid else dense,
        # No `modifier`: Qdrant's IDF modifier is a collection-wide statistic (S1).
        sparse_vectors_config={SPARSE: SparseVectorParams()} if hybrid else None,
        on_disk_payload=True,
        quantization_config=ScalarQuantization(
            scalar=ScalarQuantizationConfig(type=ScalarType.INT8, always_ram=True)),
    )

    # Embedded Qdrant ignores indexes/quantization; only the remote server uses
    # these storage/acceleration settings. Keep local correctness tests distinct.
    if config.QDRANT_URL:
        for field in (*ACL_LEVELS, "doc_id"):
            c.create_payload_index(collection_name=config.COLLECTION, field_name=field,
                                   field_schema=PayloadSchemaType.KEYWORD, wait=True)


def upsert_chunks(chunks: list[dict]):
    """chunks: [{id:int, doc_id, text, acl:[...], vector:[...], sparse:(indices, values) if hybrid}]"""
    hybrid = sparse_enabled()
    client().upsert(config.COLLECTION, wait=True, points=[
        PointStruct(id=ch["id"],
                    vector={DENSE: ch["vector"], SPARSE: SparseVector(
                        indices=ch["sparse"][0], values=ch["sparse"][1])} if hybrid else ch["vector"],
                    payload={"doc_id": ch["doc_id"], "text": ch["text"], "acl": ch["acl"], **{level: ch[level] for level in ACL_LEVELS}})
        for ch in chunks
    ])


def _acl_filter(principals: list[str]) -> Filter:
    return Filter(must=[FieldCondition(key=level, match=MatchAny(any=principals)) for level in ACL_LEVELS])


def search(query_vector: list[float], principals: list[str], top_k: int,
           sparse: tuple[list[int], list[float]] | None = None) -> list[dict]:
    """THE security boundary: acl-match filter applied inside the engine.

    With `sparse`, dense and sparse branches are fused (RRF) server-side. The
    same filter is attached to EACH prefetch, so both candidate lists are built
    from readable rows only; the fused ranking never sees a hidden chunk.
    """
    acl = _acl_filter(principals)
    if sparse is None:
        hits = client().query_points(
            collection_name=config.COLLECTION,
            query=query_vector,
            using=DENSE if sparse_enabled() else None,
            query_filter=acl,
            limit=top_k,
            score_threshold=config.MIN_SCORE,
            with_payload=True,
        ).points
    else:
        hits = client().query_points(
            collection_name=config.COLLECTION,
            prefetch=[
                Prefetch(query=query_vector, using=DENSE, filter=acl, limit=2 * top_k,
                         score_threshold=config.MIN_SCORE),
                # Sparse re-scores only dense-qualified candidates (nested prefetch), so it can
                # re-rank but never add a chunk below the MIN_SCORE floor.
                Prefetch(prefetch=Prefetch(query=query_vector, using=DENSE, filter=acl,
                                           limit=2 * top_k, score_threshold=config.MIN_SCORE),
                         query=SparseVector(indices=sparse[0], values=sparse[1]), using=SPARSE,
                         filter=acl, limit=2 * top_k),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=acl,
            limit=top_k,
            with_payload=True,
        ).points
    return [{"chunk_id": h.id, "score": h.score, **h.payload} for h in hits]


def search_unfiltered_count(query_vector: list[float], principals: list[str], top_k: int) -> int:
    """How many of the top-k relevant chunks were hidden from this user —
    the audit signal ('denied_count'). Internal only: results never leave
    this function, only the count does."""
    hits = client().query_points(
        collection_name=config.COLLECTION, query=query_vector,
        using=DENSE if sparse_enabled() else None, limit=top_k, score_threshold=config.MIN_SCORE, with_payload=list(ACL_LEVELS),
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


def update_doc_acls(acls: dict[str, list[str]], batch_size: int = 1000):
    """One filtered write per equal-grant batch; section/paragraph fields untouched."""
    from .ingest import validate_acl
    if not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    grouped = {}
    for doc_id, acl in acls.items():
        if not isinstance(doc_id, str) or not doc_id.strip():
            raise ValueError("document ids must be nonempty")
        grants = tuple(validate_acl(acl))
        grouped.setdefault(grants, []).append(doc_id)
    for grants, documents in grouped.items():
        for offset in range(0, len(documents), batch_size):
            client().set_payload(collection_name=config.COLLECTION,
                                 payload={"acl_doc": list(grants)},
                                 points=Filter(must=[FieldCondition(key="doc_id", match=MatchAny(
                                     any=documents[offset:offset + batch_size]))]), wait=True)


def close_client():
    global _client
    if _client is not None:
        _client.close()
        _client = None


atexit.register(close_client)
