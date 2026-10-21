"""S1-class: hybrid fusion must not depend on insertion order (and so not on hidden documents).

Real mail repeats paragraphs, so exact score ties are common. Qdrant breaks a tie by internal
insertion order and server-side RRF turns that order into fused scores; adding or removing a
hidden document shifts the order. Fusion is therefore done client-side as a pure function of the
visible candidates' raw scores and chunk ids (T27).
"""
import itertools
import random

import pytest

from app import vectorstore
from test_mutation_kills_tie_cut import cap_fetches
from test_mutation_kills_retrieval_models import QSPARSE, QUERY, _pt, hybrid_store  # noqa: F401


def test_rrf_fuse_is_invariant_to_the_order_of_tied_candidates():
    # Three-way tie at the top of dense, two-way tie in sparse; chunk ids are the only tie-breaker.
    dense = [(7, 0.9), (3, 0.9), (5, 0.9), (2, 0.5), (9, 0.4)]
    sparse = [(9, 2.0), (5, 1.0), (3, 1.0), (2, 0.1)]
    reference = vectorstore.rrf_fuse(dense, sparse)
    for d in itertools.permutations(dense[:3]):
        for s in itertools.permutations(sparse[1:3]):
            assert vectorstore.rrf_fuse([*d, *dense[3:]], [sparse[0], *s, sparse[3]]) == reference


def test_rrf_fuse_uses_competition_ranks_and_the_chunk_id_tiebreak():
    dense = [(7, 0.9), (3, 0.9), (5, 0.9), (2, 0.5), (9, 0.4)]  # ranks 1,1,1,4,5
    fused = vectorstore.rrf_fuse(dense)
    k = vectorstore.RRF_K
    assert dict(fused) == {3: 1 / k, 5: 1 / k, 7: 1 / k, 2: 1 / (4 + k - 1), 9: 1 / (5 + k - 1)}
    assert [cid for cid, _ in fused] == [3, 5, 7, 2, 9]  # (-fused_score, chunk_id)


def test_rrf_fuse_matches_qdrants_constant_on_untied_input():
    # Qdrant RRF (k=2): 1 / (rank + k - 1), 1-based rank, summed over lists.
    fused = dict(vectorstore.rrf_fuse([(1, 0.9), (2, 0.8)], [(2, 3.0), (1, 1.0)]))
    assert vectorstore.RRF_K == 2
    assert fused[1] == pytest.approx(1 / 2 + 1 / 3) and fused[2] == pytest.approx(1 / 3 + 1 / 2)


TIED = [_pt(100 + n, f"T{n}", 0.80, ([1], [2.0]), ["*"]) for n in range(14)]  # identical vector + sparse
DISTINCT = [_pt(1, "A", 0.90, ([1], [1.0]), ["*"]), _pt(2, "Z", 0.30, ([2], [1.0]), ["*"])]
# Hidden chunks tie with the visible ones on both legs, so they would land inside a tie group.
HIDDEN = [_pt(200 + n, f"H{n}", 0.80, ([1], [2.0]), ["group:secret"]) for n in range(5)]


def _search(load, points, top_k):
    load(points)
    return [(h["chunk_id"], h["doc_id"], h["score"])
            for h in vectorstore.search(QUERY, ["*"], top_k, sparse=QSPARSE)]


def _interleave(visible, hidden):
    out, hid = [], iter(hidden)
    for v in visible:
        out.append(v)
        out.extend(itertools.islice(hid, 1))
    return out + list(hid)


@pytest.mark.parametrize("top_k", [3, 5])  # 2*top_k < 14: the candidate cut falls inside the tie group
def test_hybrid_results_and_scores_are_identical_across_ingest_orders_and_hidden_chunks(hybrid_store, top_k):
    visible = DISTINCT + TIED
    shuffled = random.Random(7).sample(visible, len(visible))
    variants = {
        "forward": visible,
        "reversed": visible[::-1],
        "shuffled": shuffled,
        "hidden_interleaved": _interleave(visible, HIDDEN),
        "hidden_interleaved_reversed": _interleave(visible[::-1], HIDDEN),
        "hidden_first": HIDDEN + shuffled,
    }
    results = {name: _search(hybrid_store, pts, top_k) for name, pts in variants.items()}
    reference = results["forward"]
    assert len(reference) == top_k
    for name, got in results.items():
        assert got == reference, name
    # Documented rule: among exact ties the lowest chunk id wins the cut.
    assert [cid for cid, _, _ in reference][:3] == [100, 101, 102]


def test_tie_group_larger_than_the_fetch_cap_is_dropped_whole_not_cut_by_insertion_order(hybrid_store, monkeypatch):
    # 14 ties + A with a pool of 6: the cap stops the widening before a strictly lower score
    # appears, so the boundary tie group is dropped whole instead of cut by insertion order.
    cap_fetches(monkeypatch, 8)
    visible = DISTINCT + TIED
    forward = _search(hybrid_store, visible, 3)
    backward = _search(hybrid_store, visible[::-1], 3)
    assert forward == backward
    assert [cid for cid, _, _ in forward] == [1]  # only the strictly-better chunk survives
