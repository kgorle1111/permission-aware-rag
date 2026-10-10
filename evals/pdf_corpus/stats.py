"""Edit distance and intervals for the PDF evals (stdlib only)."""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Hashable, Sequence


def edit_distance(a: Sequence[Hashable], b: Sequence[Hashable]) -> int:
    """Levenshtein distance, Hyyro's bit-parallel form (python big ints): O(len(b)) big-int ops."""
    if not a or not b:
        return len(a) or len(b)
    m = len(a)
    peq: dict[Hashable, int] = {}
    for i, ch in enumerate(a):
        peq[ch] = peq.get(ch, 0) | (1 << i)
    mask, high = (1 << m) - 1, 1 << (m - 1)
    pv, mv, score = mask, 0, m
    for ch in b:
        eq = peq.get(ch, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | (~(xh | pv) & mask)
        mh = pv & xh
        if ph & high:
            score += 1
        elif mh & high:
            score -= 1
        ph = ((ph << 1) | 1) & mask
        mh = (mh << 1) & mask
        pv = mh | (~(xv | ph) & mask)
        mv = ph & xv
    return score


def _dp(a, b) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def bootstrap_ratio(num: list[float], den: list[float], iters: int = 2000, seed: int = 0):
    """Pooled ratio sum(num)/sum(den) with a 95% percentile bootstrap over pages."""
    n = len(num)
    rng = random.Random(seed)
    vals = []
    for _ in range(iters):
        idx = [rng.randrange(n) for _ in range(n)]
        d = sum(den[i] for i in idx)
        vals.append(sum(num[i] for i in idx) / d if d else 0.0)
    vals.sort()
    return (sum(num) / sum(den), vals[int(0.025 * iters)], vals[int(0.975 * iters) - 1])


def bag_recall(truth: list[str], got: list[str]) -> tuple[int, int]:
    """(truth words found in got as a multiset, truth words): order-insensitive."""
    c = Counter(got)
    hit = 0
    for w, k in Counter(truth).items():
        hit += min(k, c[w])
    return hit, len(truth)


if __name__ == "__main__":
    rng = random.Random(1)
    for _ in range(400):
        a = "".join(rng.choice("abc") for _ in range(rng.randrange(0, 25)))
        b = "".join(rng.choice("abc") for _ in range(rng.randrange(0, 25)))
        assert edit_distance(a, b) == _dp(a, b), (a, b)
    assert edit_distance(["a", "b"], ["a", "c", "b"]) == 1
    lo, hi = wilson(0, 100)
    assert lo == 0.0 and 0.03 < hi < 0.04
    print("stats self-check ok")
