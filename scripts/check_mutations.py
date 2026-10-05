"""Validate a complete mutmut 3.8 mutation-test report."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


COUNTERS = (
    "not_checked",
    "killed",
    "survived",
    "total",
    "no_tests",
    "skipped",
    "suspicious",
    "timeout",
    "check_was_interrupted_by_user",
    "segfault",
    "caught_by_type_check",
)
UNRESOLVED = (
    "not_checked",
    "survived",
    "no_tests",
    "suspicious",
    "timeout",
    "skipped",
    "check_was_interrupted_by_user",
    "segfault",
    "caught_by_type_check",
)

# Exit codes from mutmut 3.8's status_by_exit_code mapping.
STATUS_BY_EXIT_CODE = {
    1: "killed",
    3: "killed",
    0: "survived",
    5: "no_tests",
    33: "no_tests",
    2: "check_was_interrupted_by_user",
    None: "not_checked",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    -24: "timeout",
    24: "timeout",
    152: "timeout",
    255: "timeout",
    37: "caught_by_type_check",
    -11: "segfault",
    -9: "segfault",
}


class MutationReportError(ValueError):
    """Raised when a mutation report is incomplete or has unresolved results."""


def validate_report(report: Any) -> dict[str, int]:
    """Validate a normalized report containing every mutmut 3.8 counter."""
    if not isinstance(report, dict):
        raise MutationReportError("mutation report must be a JSON object")

    missing = [name for name in COUNTERS if name not in report]
    if missing:
        raise MutationReportError(
            "mutation report is missing counters: " + ", ".join(missing)
        )

    values: dict[str, int] = {}
    for name in COUNTERS:
        value = report[name]
        if type(value) is not int or value < 0:
            raise MutationReportError(
                f"mutation counter {name!r} must be a nonnegative integer"
            )
        values[name] = value

    if values["total"] == 0:
        raise MutationReportError("mutation report contains no mutants")
    outcomes = sum(values[name] for name in COUNTERS if name != "total")
    if outcomes != values["total"]:
        raise MutationReportError(
            f"incomplete mutation report: outcome counts sum to {outcomes}, "
            f"but total is {values['total']}"
        )

    unresolved = {name: values[name] for name in UNRESOLVED if values[name]}
    if unresolved:
        details = ", ".join(f"{name}={count}" for name, count in unresolved.items())
        raise MutationReportError("mutation results need review: " + details)
    return values


def derive_report_from_metadata(mutants_dir: Path) -> dict[str, int]:
    """Derive omitted mutmut 3.8 counters from every source-file .meta record."""
    paths = sorted(mutants_dir.rglob("*.py.meta"))
    if not paths:
        raise MutationReportError(
            f"no mutmut source metadata found under {mutants_dir}"
        )

    outcomes: Counter[str] = Counter()
    metadata_mutants = 0
    for path in paths:
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MutationReportError(f"cannot read mutmut metadata {path}: {exc}") from exc
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("exit_code_by_key"), dict
        ):
            raise MutationReportError(
                f"mutmut metadata {path} has no exit_code_by_key mapping"
            )
        exit_codes = metadata["exit_code_by_key"]
        if not exit_codes:
            continue
        metadata_mutants += len(exit_codes)
        for code in exit_codes.values():
            if code is not None and type(code) is not int:
                raise MutationReportError(
                    f"mutmut metadata {path} contains a non-integer exit code"
                )
            if code == 3:
                raise MutationReportError(
                    f"mutmut metadata {path} contains internal pytest error exit code 3"
                )
            # mutmut classifies unknown exit codes as suspicious.
            outcomes[STATUS_BY_EXIT_CODE.get(code, "suspicious")] += 1

    if metadata_mutants == 0:
        raise MutationReportError("mutmut metadata contains no evaluated mutants")
    return {name: (metadata_mutants if name == "total" else outcomes[name]) for name in COUNTERS}


def complete_raw_report(report: Any, mutants_dir: Path) -> dict[str, int]:
    """Complete stock mutmut 3.8 JSON using its per-source metadata records."""
    if not isinstance(report, dict):
        raise MutationReportError("mutation report must be a JSON object")

    derived = derive_report_from_metadata(mutants_dir)
    # The stock exporter omits `not_checked` and `caught_by_type_check`; every
    # field it does export must agree with the underlying mutant records.
    for name in COUNTERS:
        if name not in report:
            if name not in {"not_checked", "caught_by_type_check"}:
                raise MutationReportError(f"mutation report is missing counter: {name}")
            continue
        value = report[name]
        if type(value) is not int or value < 0:
            raise MutationReportError(
                f"mutation counter {name!r} must be a nonnegative integer"
            )
        if value != derived[name]:
            raise MutationReportError(
                f"exported counter {name}={value} disagrees with metadata "
                f"count {derived[name]}"
            )
    return derived


def check_report_file(report_path: Path, mutants_dir: Path | None = None) -> dict[str, int]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MutationReportError(f"cannot read mutation report {report_path}: {exc}") from exc

    if mutants_dir is not None:
        complete = complete_raw_report(report, mutants_dir)
    else:
        complete = report
    return validate_report(complete)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report", nargs="?", type=Path,
        default=Path("mutants/mutmut-cicd-stats.json"),
        help="mutmut export-cicd-stats JSON file",
    )
    parser.add_argument(
        "--mutants-dir", type=Path,
        help="mutmut metadata directory (required to complete stock 3.8 exports)",
    )
    args = parser.parse_args(argv)
    try:
        stats = check_report_file(args.report, args.mutants_dir)
    except MutationReportError as exc:
        print(f"mutation gate failed: {exc}", file=sys.stderr)
        return 1
    print(f"mutation gate passed: {stats['killed']}/{stats['total']} mutants killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
