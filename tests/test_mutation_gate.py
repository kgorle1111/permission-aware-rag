"""Contract tests for the strict mutation-result gate."""

import ast
import hashlib
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


def test_equivalent_count_is_bounded_and_unreviewed_survivors_still_fail(clean_report):
    with pytest.raises(check_mutations.MutationReportError, match="equivalent count"):
        check_mutations.validate_report(clean_report, equivalent_count=1)
    clean_report.update(killed=2, survived=2)
    with pytest.raises(check_mutations.MutationReportError, match="survived=1"):
        check_mutations.validate_report(clean_report, equivalent_count=1)


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


def test_report_must_be_an_object(clean_report, tmp_path: Path):
    with pytest.raises(check_mutations.MutationReportError, match="JSON object"):
        check_mutations.validate_report([])
    with pytest.raises(check_mutations.MutationReportError, match="JSON object"):
        check_mutations.complete_raw_report([], tmp_path)


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


@pytest.mark.parametrize("value", [-1, 2.5, True, "1"])
def test_raw_export_counter_types_and_sign_are_checked(tmp_path: Path, value):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "app.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"m1": 1}}), encoding="utf-8"
    )
    report = {
        "killed": value,
        "survived": 0,
        "total": 1,
        "no_tests": 0,
        "skipped": 0,
        "suspicious": 0,
        "timeout": 0,
        "check_was_interrupted_by_user": 0,
        "segfault": 0,
    }
    with pytest.raises(check_mutations.MutationReportError, match="nonnegative integer"):
        check_mutations.complete_raw_report(report, mutants_dir)


def test_missing_metadata_tree_is_not_a_complete_report(tmp_path: Path):
    with pytest.raises(check_mutations.MutationReportError, match="no mutmut source metadata"):
        check_mutations.derive_report_from_metadata(tmp_path / "empty")


def test_metadata_exit_codes_must_be_integers_or_null(tmp_path: Path):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "app.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"m1": "1"}}), encoding="utf-8"
    )
    with pytest.raises(check_mutations.MutationReportError, match="non-integer exit code"):
        check_mutations.derive_report_from_metadata(mutants_dir)


@pytest.mark.parametrize(
    "metadata, message",
    [
        ("{bad", "cannot read mutmut metadata"),
        (json.dumps({"exit_code_by_key": []}), "no exit_code_by_key mapping"),
        (json.dumps({"exit_code_by_key": {"": 0}}), "invalid mutant id"),
        (json.dumps({"exit_code_by_key": {"app.x.f": True}}), "non-integer exit code"),
    ],
)
def test_equivalence_metadata_integrity_checks(tmp_path: Path, metadata: str, message: str):
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "one.py.meta").write_text(metadata, encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match=message):
        check_mutations.load_mutant_exit_codes(mutants_dir)


def test_equivalence_metadata_rejects_missing_tree_and_duplicate_ids(tmp_path: Path):
    with pytest.raises(check_mutations.MutationReportError, match="no mutmut source metadata"):
        check_mutations.load_mutant_exit_codes(tmp_path / "empty")
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    (mutants_dir / "one.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"app.one.fn": 0}}), encoding="utf-8"
    )
    (mutants_dir / "two.py.meta").write_text(
        json.dumps({"exit_code_by_key": {"app.one.fn": 0}}), encoding="utf-8"
    )
    with pytest.raises(check_mutations.MutationReportError, match="duplicate mutant id"):
        check_mutations.load_mutant_exit_codes(mutants_dir)


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


def _equivalent_fixture(tmp_path: Path, *, exit_code=0, reason="This expression is exactly equivalent for all valid inputs."):
    source_root = tmp_path / "source"
    mutants_dir = tmp_path / "mutants"
    source_file = source_root / "app" / "sample.py"
    mutant_file = mutants_dir / "app" / "sample.py"
    source_file.parent.mkdir(parents=True)
    mutant_file.parent.mkdir(parents=True)
    source_text = "def original(value):\n    return value or 1\n"
    mutant_text = (
        "def original(value):\n    return value or 1\n\n"
        "def x__example__mutmut_1(value):\n    return value if value else 1\n"
    )
    source_file.write_text(source_text, encoding="utf-8")
    mutant_file.write_text(mutant_text, encoding="utf-8")
    mutant_id = "app.sample.x__example__mutmut_1"
    metadata = {"exit_code_by_key": {mutant_id: exit_code}}
    (mutants_dir / "app" / "sample.py.meta").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    function = next(
        node for node in ast.parse(mutant_text).body
        if isinstance(node, ast.FunctionDef) and node.name == mutant_id.rsplit(".", 1)[-1]
    )
    equivalent = {
        "version": 1,
        "mutants": {
            mutant_id: {
                "source_sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
                "mutant_sha256": hashlib.sha256(
                    ast.dump(function, include_attributes=False).encode("utf-8")
                ).hexdigest(),
                "reason": reason,
            }
        },
    }
    equivalents_path = tmp_path / "equivalents.json"
    equivalents_path.write_text(json.dumps(equivalent), encoding="utf-8")
    report_path = tmp_path / "report.json"
    raw_report = {
        "killed": 0,
        "survived": 1,
        "total": 1,
        "no_tests": 0,
        "skipped": 0,
        "suspicious": 0,
        "timeout": 0,
        "check_was_interrupted_by_user": 0,
        "segfault": 0,
    }
    if exit_code == 1:
        raw_report.update(killed=1, survived=0)
    report_path.write_text(json.dumps(raw_report), encoding="utf-8")
    return source_root, mutants_dir, equivalents_path, report_path, mutant_id


def test_hash_pinned_equivalent_survivor_passes_without_changing_score(tmp_path: Path):
    source_root, mutants_dir, allowlist, report, _ = _equivalent_fixture(tmp_path)
    stats = check_mutations.check_report_file(
        report, mutants_dir, allowlist, source_root
    )
    assert stats["killed"] == 0
    assert stats["survived"] == 1
    assert stats["total"] == 1


@pytest.mark.parametrize("field", ["source_sha256", "mutant_sha256"])
def test_equivalent_hash_mismatch_fails_for_source_or_function_ast(tmp_path: Path, field):
    source_root, mutants_dir, allowlist, report, _ = _equivalent_fixture(tmp_path)
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    mutant_id = next(iter(data["mutants"]))
    data["mutants"][mutant_id][field] = "0" * 64
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match=f"{field} mismatch"):
        check_mutations.check_report_file(report, mutants_dir, allowlist, source_root)


def test_stale_equivalent_id_is_rejected(tmp_path: Path):
    source_root, mutants_dir, allowlist, report, mutant_id = _equivalent_fixture(tmp_path)
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    entry = data["mutants"].pop(mutant_id)
    data["mutants"]["app.sample.x__deleted__mutmut_9"] = entry
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="stale or unknown"):
        check_mutations.check_report_file(report, mutants_dir, allowlist, source_root)


def test_allowlist_cannot_accept_a_killed_non_survivor(tmp_path: Path):
    source_root, mutants_dir, allowlist, report, _ = _equivalent_fixture(
        tmp_path, exit_code=1
    )
    with pytest.raises(check_mutations.MutationReportError, match="only valid for a survivor"):
        check_mutations.check_report_file(report, mutants_dir, allowlist, source_root)


@pytest.mark.parametrize("exit_code", [None, 2, 34, 36, 37, -11])
def test_allowlist_cannot_override_incomplete_or_error_statuses(tmp_path: Path, exit_code):
    source_root, mutants_dir, allowlist, _report, mutant_id = _equivalent_fixture(
        tmp_path, exit_code=exit_code
    )
    with pytest.raises(check_mutations.MutationReportError, match="only valid for a survivor"):
        check_mutations.validate_equivalents(
            allowlist, mutants_dir, source_root, {mutant_id: exit_code}
        )


def test_every_survivor_needs_an_entry_and_reason_must_be_substantive(tmp_path: Path):
    source_root, mutants_dir, allowlist, report, _ = _equivalent_fixture(tmp_path)
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    mutant_id, entry = next(iter(data["mutants"].items()))
    entry["reason"] = "equivalent"
    data["mutants"] = {mutant_id: entry}
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="meaningful reason"):
        check_mutations.check_report_file(report, mutants_dir, allowlist, source_root)


def test_equivalence_option_requires_metadata_and_versioned_schema(tmp_path: Path):
    source_root, mutants_dir, allowlist, report, _ = _equivalent_fixture(tmp_path)
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    data["version"] = 2
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="version 1"):
        check_mutations.check_report_file(report, mutants_dir, allowlist, source_root)
    with pytest.raises(check_mutations.MutationReportError, match="requires --mutants-dir"):
        check_mutations.check_report_file(report, None, allowlist, source_root)
    data["version"] = 1
    data["description"] = "stale configuration"
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="only version and mutants"):
        check_mutations.validate_equivalents(allowlist, mutants_dir, source_root)


@pytest.mark.parametrize(
    "payload, error",
    [
        ("not-json", "cannot read equivalents file"),
        (json.dumps({"version": 1, "mutants": []}), "mutants object"),
        (json.dumps({"version": 1, "mutants": {"stale": []}}), "stale or unknown"),
    ],
)
def test_equivalents_input_schema_and_unknown_ids_fail(
    tmp_path: Path, payload: str, error: str
):
    source_root, mutants_dir, allowlist, _report, mutant_id = _equivalent_fixture(tmp_path)
    custom = tmp_path / "custom-equivalents.json"
    if error == "stale or unknown":
        data = json.loads(allowlist.read_text(encoding="utf-8"))
        data["mutants"] = {"app.unknown.x__test__mutmut_1": next(iter(data["mutants"].values()))}
        payload = json.dumps(data)
    custom.write_text(payload, encoding="utf-8")
    exit_codes = {mutant_id: 0}
    with pytest.raises(check_mutations.MutationReportError, match=error):
        check_mutations.validate_equivalents(custom, mutants_dir, source_root, exit_codes)


def test_equivalent_entry_schema_and_mutant_function_are_exact(tmp_path: Path):
    source_root, mutants_dir, allowlist, _report, mutant_id = _equivalent_fixture(tmp_path)
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    entry = data["mutants"][mutant_id]
    data["mutants"][mutant_id] = {**entry, "notes": "unreviewed extra field"}
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="exactly"):
        check_mutations.validate_equivalents(
            allowlist, mutants_dir, source_root, {mutant_id: 0}
        )


def test_equivalent_entry_must_be_an_object_and_id_must_name_app_module(tmp_path: Path):
    source_root, mutants_dir, allowlist, _report, mutant_id = _equivalent_fixture(tmp_path)
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    entry = data["mutants"][mutant_id]
    data["mutants"][mutant_id] = None
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="entry must be an object"):
        check_mutations.validate_equivalents(
            allowlist, mutants_dir, source_root, {mutant_id: 0}
        )

    data["mutants"] = {"other.sample.x__fn__mutmut_1": {
        "source_sha256": "0" * 64,
        "mutant_sha256": "0" * 64,
        "reason": "This is an adequately long review rationale.",
    }}
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="invalid application mutant id"):
        check_mutations.validate_equivalents(
            allowlist, mutants_dir, source_root, {"other.sample.x__fn__mutmut_1": 0}
        )

    data["mutants"] = {mutant_id: entry}
    mutant_file = mutants_dir / "app" / "sample.py"
    mutant_file.write_text("def original(value):\n    return value or 1\n", encoding="utf-8")
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="function .* is missing"):
        check_mutations.validate_equivalents(
            allowlist, mutants_dir, source_root, {mutant_id: 0}
        )


def test_invalid_mutant_id_and_bad_hash_format_are_rejected(tmp_path: Path):
    source_root, mutants_dir, allowlist, _report, mutant_id = _equivalent_fixture(tmp_path)
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    invalid_id = "app...escape"
    data["mutants"][invalid_id] = data["mutants"].pop(mutant_id)
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="invalid application mutant id"):
        check_mutations.validate_equivalents(
            allowlist, mutants_dir, source_root, {invalid_id: 0}
        )

    data = json.loads(allowlist.read_text(encoding="utf-8"))
    data["mutants"][mutant_id] = data["mutants"].pop(invalid_id)
    data["mutants"][mutant_id]["source_sha256"] = "xyz"
    allowlist.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="invalid source_sha256"):
        check_mutations.validate_equivalents(
            allowlist, mutants_dir, source_root, {mutant_id: 0}
        )


def test_equivalent_source_file_and_generated_python_must_exist_and_parse(tmp_path: Path):
    source_root, mutants_dir, allowlist, _report, mutant_id = _equivalent_fixture(tmp_path)
    source_file = source_root / "app" / "sample.py"
    source_text = source_file.read_text(encoding="utf-8")
    source_file.unlink()
    with pytest.raises(check_mutations.MutationReportError, match="cannot read application source"):
        check_mutations.validate_equivalents(
            allowlist, mutants_dir, source_root, {mutant_id: 0}
        )
    source_file.write_text(source_text, encoding="utf-8")
    generated_file = mutants_dir / "app" / "sample.py"
    generated_file.write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="cannot parse generated mutant"):
        check_mutations.validate_equivalents(
            allowlist, mutants_dir, source_root, {mutant_id: 0}
        )


def test_allowlist_must_account_for_every_current_survivor(tmp_path: Path):
    source_root, mutants_dir, allowlist, report, mutant_id = _equivalent_fixture(tmp_path)
    metadata_path = mutants_dir / "app" / "sample.py.meta"
    metadata_path.write_text(
        json.dumps({"exit_code_by_key": {mutant_id: 0, "app.sample.another": 0}}),
        encoding="utf-8",
    )
    raw_report = json.loads(report.read_text(encoding="utf-8"))
    raw_report.update(survived=2, total=2)
    report.write_text(json.dumps(raw_report), encoding="utf-8")
    with pytest.raises(check_mutations.MutationReportError, match="account for every survivor"):
        check_mutations.check_report_file(report, mutants_dir, allowlist, source_root)


def test_cli_reports_equivalents_separately_from_killed_score(tmp_path: Path, capsys):
    source_root, mutants_dir, allowlist, report, _ = _equivalent_fixture(tmp_path)
    result = check_mutations.main(
        [
            str(report),
            "--mutants-dir",
            str(mutants_dir),
            "--equivalents",
            str(allowlist),
            "--source-root",
            str(source_root),
        ]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert "0/1 mutants killed" in output
    assert "1 reviewed equivalents" in output
    assert "equivalents remain survivors in score" in output
