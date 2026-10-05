"""Contract tests for the strict mutation-result gate."""

import json
from pathlib import Path
import runpy

import pytest

from scripts import check_mutations


@pytest.fixture
def clean_report():
    return {
        "not_checked": 0,
        "killed": 4,
        "survived": 0,
        "total": 4,
        "no_tests": 0,
        "skipped": 0,
        "suspicious": 0,
        "timeout": 0,
        "check_was_interrupted_by_user": 0,
        "segfault": 0,
        "caught_by_type_check": 0,
    }


def test_valid_full_report_passes_without_a_score_threshold(clean_report):
    assert check_mutations.validate_report(clean_report) == clean_report


@pytest.mark.parametrize(
    "counter",
    [
        "not_checked",
        "survived",
        "no_tests",
        "suspicious",
        "timeout",
        "skipped",
        "check_was_interrupted_by_user",
        "segfault",
        "caught_by_type_check",
    ],
)
def test_any_nonzero_unresolved_outcome_fails(clean_report, counter):
    clean_report[counter] = 1
    clean_report["killed"] -= 1
    with pytest.raises(check_mutations.MutationReportError, match="need review"):
        check_mutations.validate_report(clean_report)


def test_zero_mutants_and_inconsistent_outcome_totals_fail(clean_report):
    clean_report.update(total=0, killed=0)
    with pytest.raises(check_mutations.MutationReportError, match="no mutants"):
        check_mutations.validate_report(clean_report)

    clean_report.update(total=5, killed=4)
    with pytest.raises(check_mutations.MutationReportError, match="incomplete"):
        check_mutations.validate_report(clean_report)


@pytest.mark.parametrize("bad_value", [-1, 1.5, True, "0", None])
def test_counters_must_be_nonnegative_integers(clean_report, bad_value):
    clean_report["survived"] = bad_value
    with pytest.raises(check_mutations.MutationReportError, match="nonnegative integer"):
        check_mutations.validate_report(clean_report)


def test_missing_counter_is_rejected(clean_report):
    del clean_report["caught_by_type_check"]
    with pytest.raises(check_mutations.MutationReportError, match="missing counters"):
        check_mutations.validate_report(clean_report)


def test_stock_mutmut_report_is_completed_and_cross_checked_from_meta(
    tmp_path: Path,
):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    meta = {
        "exit_code_by_key": {
            "m1": 1,
            "m2": 0,
            "m3": None,
            "m4": 37,
            "m5": 902,
        }
    }
    (mutants_dir / "app.py.meta").write_text(json.dumps(meta), encoding="utf-8")
    stock_export = {
        "killed": 1,
        "survived": 1,
        "total": 5,
        "no_tests": 0,
        "skipped": 0,
        "suspicious": 1,
        "timeout": 0,
        "check_was_interrupted_by_user": 0,
        "segfault": 0,
    }

    completed = check_mutations.complete_raw_report(stock_export, mutants_dir)
    assert completed["not_checked"] == 1
    assert completed["caught_by_type_check"] == 1
    with pytest.raises(check_mutations.MutationReportError, match="need review"):
        check_mutations.validate_report(completed)


def test_export_must_match_metadata_counts(tmp_path: Path):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "app.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"m1": 1}}), encoding="utf-8"
    )
    mismatched = {
        "killed": 0,
        "survived": 0,
        "total": 1,
        "no_tests": 0,
        "skipped": 0,
        "suspicious": 0,
        "timeout": 0,
        "check_was_interrupted_by_user": 0,
        "segfault": 0,
    }
    with pytest.raises(check_mutations.MutationReportError, match="disagrees"):
        check_mutations.complete_raw_report(mismatched, mutants_dir)


def test_full_report_is_still_cross_checked_when_metadata_is_supplied(tmp_path: Path):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "app.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"m1": 0}}), encoding="utf-8"
    )
    report_path = tmp_path / "complete.json"
    report_path.write_text(
        json.dumps(
            {
                "not_checked": 0,
                "killed": 1,
                "survived": 0,
                "total": 1,
                "no_tests": 0,
                "skipped": 0,
                "suspicious": 0,
                "timeout": 0,
                "check_was_interrupted_by_user": 0,
                "segfault": 0,
                "caught_by_type_check": 0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(check_mutations.MutationReportError, match="disagrees"):
        check_mutations.check_report_file(report_path, mutants_dir)


def test_internal_pytest_error_exit_code_is_not_counted_as_killed(tmp_path: Path):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "app.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"m1": 3}}), encoding="utf-8"
    )
    with pytest.raises(check_mutations.MutationReportError, match="internal pytest error"):
        check_mutations.derive_report_from_metadata(mutants_dir)


@pytest.mark.parametrize(
    "metadata",
    [{}, {"exit_code_by_key": []}, {"exit_code_by_key": {}}],
)
def test_empty_or_malformed_metadata_cannot_claim_a_complete_campaign(
    tmp_path: Path, metadata
):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "app.py.meta").write_text(json.dumps(metadata), encoding="utf-8")
    expected = "exit_code_by_key" if metadata != {"exit_code_by_key": {}} else "no evaluated"
    with pytest.raises(check_mutations.MutationReportError, match=expected):
        check_mutations.derive_report_from_metadata(mutants_dir)


def test_metadata_file_must_be_valid_json(tmp_path: Path):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "app.py.meta").write_text("{broken", encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="cannot read mutmut metadata"):
        check_mutations.derive_report_from_metadata(mutants_dir)


def test_raw_export_must_include_all_mutmut_38_exported_fields(tmp_path: Path):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "app.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"m1": 1}}), encoding="utf-8"
    )
    with pytest.raises(check_mutations.MutationReportError, match="missing counter: killed"):
        check_mutations.complete_raw_report({"total": 1}, mutants_dir)


def test_unknown_mutmut_exit_code_is_counted_as_suspicious(tmp_path: Path):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "app.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"m1": 481}}), encoding="utf-8"
    )
    derived = check_mutations.derive_report_from_metadata(mutants_dir)
    assert derived["suspicious"] == 1
    assert derived["total"] == 1


def test_module_entrypoint_exits_nonzero_for_invalid_report(tmp_path: Path, monkeypatch, capsys):
    report = tmp_path / "bad.json"
    report.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["check_mutations.py", str(report)])
    with pytest.raises(SystemExit) as exit_error:
        runpy.run_path(str(Path(check_mutations.__file__)), run_name="__main__")
    assert exit_error.value.code == 1
    assert "mutation gate failed" in capsys.readouterr().err


def test_cli_rejects_malformed_json_and_accepts_complete_report(tmp_path: Path):
    malformed = tmp_path / "broken.json"
    malformed.write_text("not-json", encoding="utf-8")
    assert check_mutations.main([str(malformed)]) == 1

    complete = tmp_path / "complete.json"
    complete.write_text(
        json.dumps(
            {
                "not_checked": 0,
                "killed": 1,
                "survived": 0,
                "total": 1,
                "no_tests": 0,
                "skipped": 0,
                "suspicious": 0,
                "timeout": 0,
                "check_was_interrupted_by_user": 0,
                "segfault": 0,
                "caught_by_type_check": 0,
            }
        ),
        encoding="utf-8",
    )
    assert check_mutations.main([str(complete)]) == 0
