"""Load-test helpers: percentile, Wilson bound and the permission oracle must be able to fail."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals" / "load"))
from lib import Oracle, percentile, principals, wilson_upper  # type: ignore[import-not-found]  # noqa: E402  (sys.path import; evals/load is mypy-excluded)

ORACLE = Oracle.from_files(ROOT / "platform/corpus/docs.json", ROOT / "platform/corpus/permissions.jsonl")
SALARY = {"doc_id": "hr-salaries", "text": "Compensation bands: salary bands range from 90k"}
PROJ = {"doc_id": "company-strategy", "text": "Financial projections: next fiscal year revenue"}


def test_percentile_nearest_rank():
    v = list(range(1, 101))
    assert (percentile(v, 50), percentile(v, 95), percentile(v, 99)) == (50, 95, 99)
    assert percentile([], 50) is None and percentile([7], 99) == 7


def test_wilson_upper_known_values():
    assert wilson_upper(0, 100) == pytest.approx(3.7e-2, abs=1e-3)  # z^2/(n+z^2)
    assert wilson_upper(0, 0) == 1.0
    assert wilson_upper(5, 10) > 0.5 > wilson_upper(0, 10)


def test_oracle_flags_hr_doc_for_non_hr():
    assert ORACLE.violations([SALARY], principals("bob@company.com", ["hr"])) == []
    assert ORACLE.violations([SALARY], principals("alice@company.com", ["eng"])) == ["hr-salaries"]


def test_oracle_enforces_section_level_acl():
    # doc is public ("*") but the projections section needs group:finance
    assert ORACLE.violations([PROJ], principals("alice@company.com", ["eng"])) == ["company-strategy"]
    assert ORACLE.violations([PROJ], principals("fin@company.com", ["finance"])) == []


def test_oracle_rejects_unknown_text():
    forged = {"doc_id": "handbook", "text": "something the corpus never said"}
    assert ORACLE.violations([forged], principals("ceo@company.com", ["board"])) == ["handbook"]
