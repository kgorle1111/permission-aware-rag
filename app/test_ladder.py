"""The ladder harness must accept the baseline and reject every known-leaky rung."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals import ladder  # noqa: E402

RUNGS = ladder.default_rungs()
RESULTS = {r.name: ladder.evaluate(r) for r in RUNGS}


def test_baseline_is_accepted_with_clean_gates():
    base = RESULTS[RUNGS[0].name]
    assert not base["rejected"]
    assert (base["leaks"], base["isolation_diffs"], base["reasons"]) == (0, 0, [])
    assert base["recall_hits"] == base["recall_total"] > 0
    assert 0 < base["p50_ms"] <= base["p95_ms"]


CONTROLS = [r for r in RUNGS if r.control]
CANDIDATES = [r for r in RUNGS[1:] if not r.control]


@pytest.mark.parametrize("rung", CANDIDATES, ids=lambda r: r.name)
def test_every_tier3_rung_passes_every_gate(rung):
    result = RESULTS[rung.name]
    assert not result["rejected"] and (result["leaks"], result["isolation_diffs"]) == (0, 0)


@pytest.mark.parametrize("rung", CONTROLS, ids=lambda r: r.name)
def test_every_mutant_is_rejected(rung):
    result = RESULTS[rung.name]
    assert rung.control and result["rejected"] and result["reasons"]


def test_rejection_ignores_quality():
    # FlattenIntersection keeps 100% app recall yet is rejected: quality cannot buy back a gate failure.
    flat = RESULTS["FlattenIntersection"]
    assert flat["recall_hits"] == flat["recall_total"] and flat["rejected"]


def test_check_flags_accepted_control_and_rejected_baseline():
    results = list(RESULTS.values())
    assert ladder.check(results) == []
    accepted_control = [results[0], {**results[0], "name": "FakeLeaky", "control": True}]
    assert ladder.check(accepted_control) == ["negative control accepted: FakeLeaky"]
    rejected_baseline = [{**results[0], "rejected": True, "reasons": ["3 leaks"]}]
    assert ladder.check(rejected_baseline) == ["baseline rejected: 3 leaks"]


def test_table_marks_verdicts():
    text = ladder.table(list(RESULTS.values()))
    assert text.count("| REJECTED |") == len(CONTROLS)
    assert text.count("| accepted |") == 1 + len(CANDIDATES)
