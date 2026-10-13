"""Does Qdrant apply the outer ACL filter inside nested prefetches? Embedded engine vs a real server.

Hybrid search builds a dense candidate stage nested under a sparse rescore. If an engine ran that
inner stage unfiltered, hidden chunks could fill the candidate pool and change what a user sees.
platform/app/vectorstore.py sends the ACL filter at every level anyway (asserted by
platform/tests/test_mutation_kills_retrieval_models.py); this script checks the engine itself.

    docker run -d -p 127.0.0.1:6339:6333 qdrant/qdrant:v1.19.1
    python evals/verify_qdrant_nested_filter.py http://127.0.0.1:6339   # needs qdrant-client
"""

import sys

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchAny,
    PointStruct,
    Prefetch,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

LEVELS = ("acl_doc", "acl_section", "acl_para")
QUERY, QUERY_SPARSE, K = [1.0, 0.0, 0.0, 0.0], SparseVector(indices=[1], values=[1.0]), 1


def acl(principals: list[str]) -> Filter:
    return Filter(must=[FieldCondition(key=level, match=MatchAny(any=principals)) for level in LEVELS])


def point(pid: int, dense: list[float], sparse: tuple[list[int], list[float]], who: str) -> PointStruct:
    return PointStruct(
        id=pid,
        vector={"dense": dense, "sparse": SparseVector(indices=sparse[0], values=sparse[1])},
        payload={level: [who] for level in LEVELS},
    )


VISIBLE = [point(1, [1, 0.2, 0, 0], ([1], [1.0]), "pub"), point(2, [0.5, 1, 0, 0], ([2], [1.0]), "pub")]
# dense-closer and sparse-stronger than every visible point
HIDDEN = [point(100 + j, [1, 0.01 * j, 0, 0], ([1], [5.0]), "secret") for j in range(6)]


def build(client: QdrantClient, name: str, points: list[PointStruct]) -> None:
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        name,
        vectors_config={"dense": VectorParams(size=4, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams()},
    )
    client.upsert(name, points, wait=True)


def hybrid(client: QdrantClient, name: str, inner_filter: bool) -> list[tuple[int, float]]:
    """The platform's hybrid query shape; inner_filter=False is the dropped-filter mutant."""
    a = acl(["pub"])
    inner = Prefetch(query=QUERY, using="dense", filter=a if inner_filter else None, limit=2 * K)
    points = client.query_points(
        name,
        prefetch=[
            Prefetch(query=QUERY, using="dense", filter=a, limit=2 * K),
            Prefetch(prefetch=inner, query=QUERY_SPARSE, using="sparse", filter=a, limit=2 * K),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        query_filter=a,
        limit=K,
    ).points
    return [(int(p.id), round(p.score, 6)) for p in points]


def main(url: str) -> None:
    for label, client in (("local", QdrantClient(":memory:")), ("server", QdrantClient(url=url))):
        build(client, "with_hidden", VISIBLE + HIDDEN)
        build(client, "no_hidden", VISIBLE)
        for inner in (True, False):
            a, b = hybrid(client, "with_hidden", inner), hybrid(client, "no_hidden", inner)
            print(f"{label:6} inner_filter={inner!s:5} with_hidden={a} no_hidden={b} invariant={a == b}")
        # isolate the mechanism: a filtered sparse rescore over an UNFILTERED dense candidate stage
        control = [
            p.id for p in client.query_points("with_hidden", query=QUERY, using="dense", limit=2).points
        ]
        nested = client.query_points(
            "with_hidden",
            prefetch=Prefetch(query=QUERY, using="dense", filter=None, limit=2),
            query=QUERY_SPARSE,
            using="sparse",
            query_filter=acl(["pub"]),
            limit=2,
        ).points
        verdict = (
            "PROPAGATED into the candidate stage" if nested else "NOT propagated (candidates were hidden)"
        )
        print(
            f"{label:6} unfiltered dense top-2 = {control}; nested result for 'pub' = {[p.id for p in nested]} => {verdict}"
        )


if __name__ == "__main__":
    main(sys.argv[1])
