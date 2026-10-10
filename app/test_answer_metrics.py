"""Hand-built pass/fail cases with exact expected values for the answer metrics (offline judge)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.metrics import (  # noqa: E402
    JudgeError,
    answer_relevancy,
    context_precision,
    context_recall,
    faithfulness,
    offline_judge,
    split_claims,
)

CTX = ["The policy limit is 2 million dollars. Renewal requires broker approval."]


def test_split_claims():
    assert split_claims("One thing. Two things!  Three? ") == ["One thing.", "Two things!", "Three?"]
    assert split_claims("   ") == []


def test_faithfulness_all_supported():
    answer = "The policy limit is 2 million dollars. Renewal requires broker approval."
    assert faithfulness(offline_judge, answer, CTX) == 1.0


def test_faithfulness_one_of_three_supported():
    answer = (
        "The policy limit is 2 million dollars. Underwriters approved the waiver yesterday. "
        "Claims are paid within ten days."
    )
    assert faithfulness(offline_judge, answer, CTX) == 1 / 3


def test_faithfulness_nothing_supported_is_zero():
    assert faithfulness(offline_judge, "Claims are paid within ten days.", CTX) == 0.0


def test_answer_relevancy_half():
    answer = "The policy limit is 2 million dollars. Our office has a nice garden."
    assert answer_relevancy(offline_judge, "What is the policy limit?", answer) == 0.5


def test_context_precision_is_position_weighted():
    gt = "The policy limit is 2 million dollars."
    relevant = "Policy limit is 2 million dollars per claim."
    late = ["Weather report for Tuesday.", relevant, "Unrelated memo.", "The limit: million dollars policy."]
    assert context_precision(offline_judge, gt, late) == (1 / 2 + 2 / 4) / 2
    early = [relevant, "The limit: million dollars policy.", "Weather report for Tuesday.", "Unrelated memo."]
    assert context_precision(offline_judge, gt, early) == 1.0


def test_context_precision_none_relevant():
    assert (
        context_precision(offline_judge, "The policy limit is 2 million dollars.", ["Weather report."]) == 0.0
    )


def test_context_recall():
    gt = "The policy limit is 2 million dollars. Renewal requires broker approval."
    limit, renewal = "Policy limit: 2 million dollars.", "Renewal requires broker approval."
    assert context_recall(offline_judge, gt, [limit]) == 0.5
    assert context_recall(offline_judge, gt, [limit, renewal]) == 1.0
    assert context_recall(offline_judge, gt, ["Weather report."]) == 0.0


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("Sure! Here you go: true", "not valid JSON"),
        (None, "not valid JSON"),
        ('{"verdicts": [1]}', "list of booleans"),
        ('{"nope": [true]}', "list of booleans"),
        ('{"verdicts": [true, false]}', "returned 2 verdicts for 1 items"),
        ("[true]", "list of booleans"),
    ],
)
def test_malformed_judge_output_is_a_prescriptive_error(raw, message):
    with pytest.raises(JudgeError, match=message):
        faithfulness(lambda _prompt: raw, "One claim.", CTX)


def test_empty_inputs_are_errors_not_scores():
    with pytest.raises(ValueError, match="answer is empty"):
        faithfulness(offline_judge, "  ", CTX)
    with pytest.raises(ValueError, match="contexts is empty"):
        context_precision(offline_judge, "gt.", [])
    with pytest.raises(ValueError, match="ground_truth is empty"):
        context_recall(offline_judge, "", CTX)
