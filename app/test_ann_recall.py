"""Pure helpers of evals/pgvector_ann_recall.py: the recall metric, percentile, vector and ACL oracle.

The harness itself needs Postgres; these pin the pieces whose bugs would silently flatter the result.
"""

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
psycopg = pytest.importorskip("psycopg")  # the module imports pgvector_rag at top level
from evals.pgvector_ann_recall import (  # noqa: E402
    BITS,
    LEVELS,
    acl_for,
    draw_mask,
    draw_vector,
    percentile,
    recall_at_k,
    summarize,
    to_vec,
    unit,
    visible,
)


def test_recall_counts_only_exact_neighbours_found() -> None:
    assert recall_at_k(["a", "b", "x"], ["a", "b", "c", "d"]) == 0.5
    assert recall_at_k([], ["a"]) == 0.0
    assert recall_at_k(["z"], []) == 1.0  # nothing to find


def test_percentile_nearest_rank() -> None:
    v = [float(i) for i in range(1, 101)]
    assert (percentile(v, 0.5), percentile(v, 0.95)) == (50.0, 95.0)
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_summarize_reports_short_and_empty_answers() -> None:
    truth = [["a", "b"], ["c", "d"]]
    s = summarize([["a", "b"], []], truth, [1.0, 3.0], k=2)
    assert (s["recall_at_k"], s["min_recall"], s["empty_queries"]) == (0.5, 0.0, 1)
    assert (s["mean_returned"], s["mean_exact_returned"]) == (1.0, 2.0)


def test_draw_vector_is_unit_and_near_its_centre() -> None:
    rnd = random.Random(1)
    centre = unit([rnd.gauss(0, 1) for _ in range(256)])
    v = draw_vector(rnd, centre)
    assert math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-9)
    assert 0.7 < sum(a * b for a, b in zip(v, centre, strict=True)) < 0.95
    assert to_vec([0.5, -1.0]) == "[0.50000,-1.00000]"


def test_visibility_oracle_matches_the_acl_it_describes() -> None:
    users = {g: u for g, (u, _) in zip(BITS, LEVELS.values(), strict=True)}  # v1 -> user in group v1, ...
    assert acl_for(0) == ["group:none"] and not any(visible(0, u) for u in users.values())
    mask = BITS["v1"] | BITS["v60"]
    assert acl_for(mask) == ["group:v1", "group:v60"]
    assert [visible(mask, users[g]) for g in BITS] == [True, False, True]


def test_draw_mask_hits_target_visibility() -> None:
    rnd = random.Random(7)
    masks = [draw_mask(rnd) for _ in range(20_000)]
    for (user, p), _ in zip(LEVELS.values(), BITS, strict=True):
        assert sum(visible(m, user) for m in masks) / len(masks) == pytest.approx(p, abs=0.01)
