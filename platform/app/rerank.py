"""Local cross-encoder reranking of ACL-filtered candidates.

Callers pass ONLY hits that already survived the engine-side ACL filter; the
model scores each (query, text) pair independently, so no ranking statistic
spans rows the caller cannot read. Runs in-process: document text never
leaves the machine.
"""
from __future__ import annotations

from . import config, embeddings


def rerank(query: str, hits: list[dict], k: int) -> list[dict]:
    """Best-first by cross-encoder score (ties by chunk id), floor-filtered, top k.

    The returned hits carry the cross-encoder logit as `score`.
    """
    if config.RERANKER != "fastembed":
        raise ValueError("invalid reranker backend")
    model = embeddings._load_fastembed("rerank", config.RERANK_MODEL or "Xenova/ms-marco-MiniLM-L-6-v2")
    scores = [float(s) for s in model.rerank(query, [h["text"] for h in hits])]
    if len(scores) != len(hits):
        raise ValueError(f"reranker returned {len(scores)} scores for {len(hits)} candidates")
    ranked = sorted(zip(scores, hits), key=lambda p: (-p[0], p[1]["chunk_id"]))
    return [{**h, "score": s} for s, h in ranked if s >= config.RERANK_MIN_SCORE][:k]
