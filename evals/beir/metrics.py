"""Retrieval metrics and tenant helpers for the BEIR eval. Stdlib only.

nDCG follows trec_eval/pytrec_eval `ndcg_cut` (what BEIR reports): linear gain = qrel grade,
discount 1/log2(rank+1), ideal ranking built from all judged docs with grade > 0.
"""

import hashlib
import math
import random
from collections.abc import Callable, Sequence

Qrels = dict[str, int]  # doc_id -> graded relevance for one query


def ndcg_at_k(ranked: Sequence[str], qrels: Qrels, k: int = 10) -> float:
    gains = [qrels.get(d, 0) for d in ranked[:k]]
    dcg = sum(max(g, 0) / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = sorted((g for g in qrels.values() if g > 0), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def recall_at_k(ranked: Sequence[str], qrels: Qrels, k: int = 100) -> float:
    rel = {d for d, g in qrels.items() if g > 0}
    return len(rel & set(ranked[:k])) / len(rel) if rel else 0.0


def paired_bootstrap_ci(
    a: Sequence[float], b: Sequence[float] | None = None, n: int = 10_000, seed: int = 0
) -> tuple[float, float, float]:
    """(mean, lo, hi) 95% percentile CI over queries; with `b`, the CI of mean(a - b) resampling pairs."""
    xs = list(a) if b is None else [x - y for x, y in zip(a, b, strict=True)]
    rng = random.Random(seed)
    m = len(xs)
    means = sorted(sum(xs[rng.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return sum(xs) / m, means[int(0.025 * n)], means[int(0.975 * n) - 1]


def wilson_upper(events: int, trials: int, z: float = 1.96) -> float:
    """Upper bound of the Wilson score interval; the honest ceiling on a leak rate when events == 0."""
    if trials == 0:
        return 1.0
    p = events / trials
    d = 1 + z * z / trials
    c = p + z * z / (2 * trials)
    return (c + z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))) / d


def tenant_of(doc_id: str, k: int = 4, seed: int = 0) -> int:
    """Synthetic ACL: seeded hash partition. Real ACLs are not random; this only exercises the filter."""
    return int.from_bytes(hashlib.sha256(f"{seed}:{doc_id}".encode()).digest()[:8], "big") % k


def query_tenant(qrels: Qrels, tenant: Callable[[str], int]) -> int | None:
    """Tenant holding most of the query's relevant docs (ties -> lowest id); None if no relevant docs."""
    counts: dict[int, int] = {}
    for d, g in qrels.items():
        if g > 0:
            counts[tenant(d)] = counts.get(tenant(d), 0) + 1
    return min(counts, key=lambda t: (-counts[t], t)) if counts else None


def restrict_qrels(qrels: Qrels, keep: Callable[[str], bool]) -> Qrels:
    return {d: g for d, g in qrels.items() if keep(d)}


def same_ranking(a: Sequence[tuple[str, float]], b: Sequence[tuple[str, float]]) -> bool:
    """Isolation check: identical ids, order AND scores (exact; both sides use the same arithmetic)."""
    return list(a) == list(b)
