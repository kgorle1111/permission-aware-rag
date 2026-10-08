"""The scale harness oracle must not silently flatten hierarchy or permit empty ACLs."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "scale_harness", Path(__file__).resolve().parents[1] / "evals" / "benchmark_scale.py"
)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def test_generated_hierarchy_requires_each_distinct_membership():
    policy = harness.policy_for(1, 1)
    principals = ["user:analyst", "group:approved", "group:bucket-1"]
    assert harness.allowed(policy, principals)
    for removed in principals:
        assert not harness.allowed(policy, [p for p in principals if p != removed])
    assert not harness.allowed({**policy, "acl_para": []}, principals)
    del policy["acl_para"]
    assert not harness.allowed(policy, principals)


def test_percentile_nearest_rank_and_empty_control():
    assert harness.percentile([1, 9, 3, 2], 0.95) == 9
    with pytest.raises(ValueError):
        harness.percentile([], 0.95)
