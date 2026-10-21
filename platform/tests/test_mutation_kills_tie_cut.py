"""Pins `vectorstore._dense_pool`'s tie-complete cut (kills mutmut_30/31/32 of x__dense_pool).

Scores are exact cosines: `_pt(id, doc, cos, ...)` points sharing `cos` tie exactly on the dense
leg. The engine cuts ties by insertion order, so every scenario is run over several ingest orders
and must return the same ids: the lowest chunk ids of the boundary tie group.
"""
import random

import pytest

from app import vectorstore
from test_mutation_kills_retrieval_models import QUERY, _pt, hybrid_store  # noqa: F401

ACL = ["*"]


def _pool(load, points, pool):
    load(points)
    return [h.id for h in vectorstore._dense_pool(QUERY, vectorstore._acl_filter(ACL), pool)]


def _orders(points):
    return {"forward": points, "reversed": points[::-1],
            "shuffled": random.Random(11).sample(points, len(points))}


def _assert_stable(load, points, pool, expected):
    for name, pts in _orders(points).items():
        assert _pool(load, pts, pool) == expected, name


def cap_fetches(monkeypatch, cap, max_calls=30):
    """Shrink MAX_TIE_FETCH and fail fast if widening stops terminating (_dense_pool 34: `>=` -> `>`).

    Any test that lowers the cap can reach the cap branch; without a bound, a loop that never
    returns hangs the suite, and mutmut reports a timeout instead of a kill depending on test order.
    """
    monkeypatch.setattr(vectorstore, "MAX_TIE_FETCH", cap)
    real, calls = vectorstore.client, []

    class Bounded:
        def __init__(self, inner):
            self._inner = inner

        def query_points(self, *args, **kwargs):
            calls.append(kwargs.get("limit"))
            if len(calls) > max_calls:
                raise AssertionError(f"tie widening did not terminate: limits {calls[-8:]}")
            return self._inner.query_points(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(vectorstore, "client", lambda: Bounded(real()))
    return calls


def _pts(spec):
    """spec: list of (id, cos)."""
    return [_pt(i, f"D{i}", cos, ([1], [1.0]), ["*"]) for i, cos in spec]


def test_tie_group_straddling_the_boundary_is_completed_not_cut_by_insertion_order(hybrid_store):
    # pool=4: positions 3 and 4 (pool-1, pool) tie with 5 and 6; position pool-2 is strictly higher,
    # so a boundary taken from hits[pool-2] would wrongly look "already proven" (mutmut_30).
    spec = [(1, 0.95), (2, 0.90), (3, 0.85),
            (10, 0.70), (11, 0.70), (12, 0.70), (13, 0.70), (14, 0.70), (15, 0.70),
            (20, 0.40)]
    _assert_stable(hybrid_store, _pts(spec), 4, [1, 2, 3, 10])


def test_tie_group_beginning_exactly_at_the_last_pool_slot(hybrid_store):
    spec = [(1, 0.95), (2, 0.90), (3, 0.85),
            (10, 0.70), (11, 0.70), (12, 0.70), (13, 0.70), (20, 0.40)]
    _assert_stable(hybrid_store, _pts(spec), 4, [1, 2, 3, 10])


def test_tie_group_ending_exactly_at_the_last_pool_slot_is_kept_whole(hybrid_store):
    spec = [(1, 0.95), (2, 0.90), (10, 0.70), (11, 0.70), (20, 0.40), (21, 0.30)]
    _assert_stable(hybrid_store, _pts(spec), 4, [1, 2, 10, 11])


def test_unambiguous_boundary_under_a_tight_cap_keeps_the_full_pool(hybrid_store, monkeypatch):
    # The strictly lower score at position `pool` proves the boundary on the first fetch (limit ==
    # cap == pool+1). Failing to stop there (mutmut_31: hits[1]; mutmut_32: hits[-2]) falls through to
    # the cap branch, which drops the boundary score even though no tie exists.
    cap_fetches(monkeypatch, 5)
    spec = [(1, 0.95), (2, 0.90), (3, 0.85), (4, 0.80), (5, 0.60), (6, 0.50), (7, 0.40)]
    _assert_stable(hybrid_store, _pts(spec), 4, [1, 2, 3, 4])


def test_unambiguous_boundary_stops_widening_before_a_larger_cap(hybrid_store, monkeypatch):
    cap_fetches(monkeypatch, 8)
    spec = [(n, 0.95 - 0.05 * n) for n in range(1, 11)]  # ten distinct scores > cap
    _assert_stable(hybrid_store, _pts(spec), 4, [1, 2, 3, 4])


def test_all_tied_set_over_the_cap_is_dropped_whole_for_every_ingest_order(hybrid_store, monkeypatch):
    cap_fetches(monkeypatch, 8)
    spec = [(1, 0.95)] + [(100 + n, 0.70) for n in range(14)]
    _assert_stable(hybrid_store, _pts(spec), 4, [1])


# _dense_pool 34 (`limit >= MAX_TIE_FETCH` -> `>`): at the cap the loop never returns. Bound the
# number of fetches so an unbounded loop fails fast instead of hanging the suite (and mutmut).
def test_widening_stops_at_the_cap_within_a_bounded_number_of_fetches(hybrid_store, monkeypatch):
    monkeypatch.setattr(vectorstore, "MAX_TIE_FETCH", 8)
    spec = [(1, 0.95)] + [(100 + n, 0.70) for n in range(14)]
    hybrid_store(_pts(spec))
    real, calls = vectorstore.client(), []

    class Bounded:
        def query_points(self, *args, **kwargs):
            calls.append(kwargs.get("limit"))
            if len(calls) > 6:
                raise AssertionError(f"tie widening did not stop at the cap: limits {calls}")
            return real.query_points(*args, **kwargs)

    monkeypatch.setattr(vectorstore, "client", Bounded)
    ids = [h.id for h in vectorstore._dense_pool(QUERY, vectorstore._acl_filter(ACL), 4)]
    assert ids == [1] and calls == [5, 8]
