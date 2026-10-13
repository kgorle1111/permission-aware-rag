"""BEIR eval helpers: nDCG/recall hand-worked, tenant partition, isolation comparison."""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.beir.metrics import (  # noqa: E402
    ndcg_at_k,
    paired_bootstrap_ci,
    query_tenant,
    recall_at_k,
    restrict_qrels,
    same_ranking,
    tenant_of,
    wilson_upper,
)


def test_ndcg_hand_worked():
    qrels = {"a": 2, "b": 1, "c": 1}
    # ranking x, a, b: DCG = 0 + 2/log2(3) + 1/log2(4); ideal a, b, c: 2 + 1/log2(3) + 1/2
    dcg = 2 / math.log2(3) + 1 / 2
    idcg = 2 + 1 / math.log2(3) + 1 / 2
    assert ndcg_at_k(["x", "a", "b"], qrels, 10) == pytest.approx(dcg / idcg)
    assert ndcg_at_k(["a", "b", "c"], qrels, 10) == pytest.approx(1.0)
    assert ndcg_at_k(["x", "y"], qrels, 10) == 0.0
    assert ndcg_at_k(["a"], {"a": 0}, 10) == 0.0  # no relevant docs: defined as 0


def test_ndcg_cutoff_and_ideal_truncation():
    qrels = {"a": 1, "b": 1}
    assert ndcg_at_k(["x", "a", "b"], qrels, 1) == 0.0
    assert ndcg_at_k(["a", "x", "b"], qrels, 1) == pytest.approx(1.0)  # ideal also cut at k


def test_recall():
    qrels = {"a": 1, "b": 2, "c": 0}
    assert recall_at_k(["a", "x", "c"], qrels, 3) == 0.5  # c has grade 0, not relevant
    assert recall_at_k(["a", "b"], qrels, 1) == 0.5


def test_tenant_partition_is_deterministic_seeded_and_balanced():
    ids = [f"d{i}" for i in range(4000)]
    parts = [tenant_of(i, 4, 0) for i in ids]
    assert parts == [tenant_of(i, 4, 0) for i in ids]
    assert parts != [tenant_of(i, 4, 1) for i in ids]
    for t in range(4):
        assert 800 < parts.count(t) < 1200


def test_query_tenant_majority_ties_and_restrict():
    tenants = {"a": 2, "b": 2, "c": 1, "d": 1, "e": 3}

    def t(doc: str) -> int:
        return tenants[doc]

    assert query_tenant({"a": 1, "b": 1, "c": 1}, t) == 2
    assert query_tenant({"c": 1, "a": 1}, t) == 1  # tie -> lowest tenant id
    assert query_tenant({"a": 0}, t) is None
    assert restrict_qrels({"a": 1, "c": 1}, lambda d: t(d) == 2) == {"a": 1}


def test_same_ranking_checks_scores_and_order():
    assert same_ranking([("a", 1.5), ("b", 1.0)], [("a", 1.5), ("b", 1.0)])
    assert not same_ranking([("a", 1.5)], [("a", 1.5000001)])
    assert not same_ranking([("a", 1.0), ("b", 1.0)], [("b", 1.0), ("a", 1.0)])


def test_wilson_upper_zero_events():
    assert wilson_upper(0, 300) == pytest.approx(3.84 / (300 + 3.84), rel=1e-3)
    assert wilson_upper(0, 0) == 1.0


def test_bootstrap_ci_contains_mean_and_is_seeded():
    xs = [0.0, 1.0, 0.5, 0.25, 0.75] * 20
    m, lo, hi = paired_bootstrap_ci(xs, seed=1)
    assert lo < m < hi
    assert (m, lo, hi) == paired_bootstrap_ci(xs, seed=1)
    assert paired_bootstrap_ci(xs, xs) == (0.0, 0.0, 0.0)
