"""Scoring contracts of the EnronQA rung harness: the oracle must be able to fail. No models, no data."""
from scripts import rung_enronqa as re_


def test_first_rank_is_one_based_and_none_when_absent():
    top = [["a", 0.9], ["b", 0.8], ["b", 0.7]]
    assert re_.first_rank(top, "a") == 1
    assert re_.first_rank(top, "b") == 2
    assert re_.first_rank(top, "z") is None
    assert re_.first_rank([], "a") is None


def test_owner_metrics_hits_mrr_and_clusters():
    m = re_.owner_metrics([1, 3, None, 12], ["x", "x", "y", "y"])
    assert [m[f"hit@{k}"]["k"] for k in (1, 4, 10)] == [1, 2, 2]  # rank 12 is outside the top 10
    assert abs(m["mrr@10"]["mean"] - (1 + 1 / 3) / 4) < 1e-9
    lo, hi = m["hit@4"]["cluster_bootstrap95"]
    assert 0 <= lo <= 0.5 <= hi <= 1


def test_leak_metrics_flags_unreadable_and_unknown_documents():
    readers = {"d1": {"alice"}, "d2": {"bob"}}
    clean = re_.leak_metrics([("alice", [["d1", 1.0]]), ("bob", [])], readers)
    assert clean["k"] == 0 and clean["n"] == 2 and clean["attempts_returning_anything"] == 1
    leaky = re_.leak_metrics([("alice", [["d1", 1.0], ["d2", 0.5]]), ("alice", [["ghost", 0.1]])], readers)
    assert leaky["k"] == 2 and leaky["leaked_chunks"] == 2  # unknown doc fails closed
    assert leaky["upper95"] > 0.5


def test_same_up_to_ties_allows_tie_reordering_but_not_score_or_member_changes():
    a = [("d1", "t", 1.0), ("d2", "t", 1.0), ("d3", "t", 0.5)]
    assert re_.same_up_to_ties(a, [("d2", "t", 1.0), ("d1", "t", 1.0), ("d3", "t", 0.5)])
    assert not re_.same_up_to_ties(a, [("d1", "t", 1.0), ("d2", "t", 0.9), ("d3", "t", 0.5)])  # score moved
    assert not re_.same_up_to_ties(a, [("d1", "t", 1.0), ("d9", "t", 1.0), ("d3", "t", 0.5)])  # member swapped
    assert re_.same_up_to_ties(a, [("d1", "t", 1.0), ("d2", "t", 1.0), ("d4", "t", 0.5)])  # last group may be cut


def test_paired_diff_is_a_per_question_difference():
    a, b, cl = [1, 1, None, 2], [2, None, None, 2], ["x", "x", "y", "y"]
    assert re_.paired_diff(a, b, cl, 1)["mean"] == 0.5  # a wins on 2 of 4 questions, never loses
    assert re_.paired_diff(a, a, cl, 10)["cluster_bootstrap95"] == [0.0, 0.0]
    assert abs(re_.paired_diff(a, b, cl, None)["mean"] - ((1 - 0.5) + (1 - 0) + 0 + 0) / 4) < 1e-9


def test_enronqa_import_does_not_shadow_platform_app():
    re_.enron()
    import app.config  # platform package, not repo-root app/
    assert app.config.__file__.startswith(str(re_.PROJECT_ROOT))
