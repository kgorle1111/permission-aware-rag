"""Validate a complete mutmut 3.8 mutation-test report."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
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


def validate_report(report: Any, equivalent_count: int = 0) -> dict[str, int]:
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
    if type(equivalent_count) is not int or not 0 <= equivalent_count <= values["survived"]:
        raise MutationReportError("equivalent count must be between zero and survived count")
    outcomes = sum(values[name] for name in COUNTERS if name != "total")
    if outcomes != values["total"]:
        raise MutationReportError(
            f"incomplete mutation report: outcome counts sum to {outcomes}, "
            f"but total is {values['total']}"
        )

    unresolved = {name: values[name] for name in UNRESOLVED if values[name]}
    if equivalent_count:
        unresolved["survived"] = values["survived"] - equivalent_count
        if not unresolved["survived"]:
            del unresolved["survived"]
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


def load_mutant_exit_codes(mutants_dir: Path) -> dict[str, int | None]:
    """Load the per-mutant status codes needed to validate exact equivalents."""
    paths = sorted(mutants_dir.rglob("*.py.meta"))
    if not paths:
        raise MutationReportError(
            f"no mutmut source metadata found under {mutants_dir}"
        )
    exit_codes: dict[str, int | None] = {}
    for path in paths:
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MutationReportError(f"cannot read mutmut metadata {path}: {exc}") from exc
        entries = metadata.get("exit_code_by_key") if isinstance(metadata, dict) else None
        if not isinstance(entries, dict):
            raise MutationReportError(
                f"mutmut metadata {path} has no exit_code_by_key mapping"
            )
        for mutant_id, code in entries.items():
            if not isinstance(mutant_id, str) or not mutant_id:
                raise MutationReportError(f"mutmut metadata {path} contains an invalid mutant id")
            if code is not None and type(code) is not int:
                raise MutationReportError(
                    f"mutmut metadata {path} contains a non-integer exit code"
                )
            if mutant_id in exit_codes:
                raise MutationReportError(f"duplicate mutant id in metadata: {mutant_id}")
            exit_codes[mutant_id] = code
    return exit_codes


def _module_file(mutant_id: str) -> tuple[Path, str]:
    if not mutant_id.startswith("app.") or "." not in mutant_id:
        raise MutationReportError(f"invalid application mutant id: {mutant_id}")
    parts = mutant_id.split(".")
    module_parts, function_name = parts[:-1], parts[-1]
    if not function_name or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part)
                                for part in module_parts):
        raise MutationReportError(f"invalid application mutant id: {mutant_id}")
    return Path(*module_parts).with_suffix(".py"), function_name


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mutant_function_hash(path: Path, function_name: str) -> str:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise MutationReportError(f"cannot parse generated mutant source {path}: {exc}") from exc
    function = next(
        (node for node in tree.body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name == function_name),
        None,
    )
    if function is None:
        raise MutationReportError(
            f"generated mutant function {function_name!r} is missing from {path}"
        )
    canonical_ast = ast.dump(function, include_attributes=False).encode("utf-8")
    return _sha256(canonical_ast)


def validate_equivalents(
    equivalents_path: Path,
    mutants_dir: Path,
    source_root: Path,
    exit_codes: dict[str, int | None] | None = None,
) -> set[str]:
    """Validate hash-pinned equivalents against source, AST, and survivor status."""
    try:
        data = json.loads(equivalents_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MutationReportError(f"cannot read equivalents file {equivalents_path}: {exc}") from exc
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise MutationReportError("equivalents file must declare version 1")
    if set(data) != {"version", "mutants"}:
        raise MutationReportError("equivalents file must contain only version and mutants")
    entries = data.get("mutants")
    if not isinstance(entries, dict):
        raise MutationReportError("equivalents file must contain a mutants object")

    status_by_id = exit_codes if exit_codes is not None else load_mutant_exit_codes(mutants_dir)
    accepted: set[str] = set()
    for mutant_id, entry in entries.items():
        if mutant_id not in status_by_id:
            raise MutationReportError(f"stale or unknown equivalent mutant id: {mutant_id}")
        if status_by_id[mutant_id] != 0:
            raise MutationReportError(
                f"equivalent entry is only valid for a survivor: {mutant_id}"
            )
        if not isinstance(entry, dict):
            raise MutationReportError(f"equivalent entry must be an object: {mutant_id}")
        expected_fields = {"source_sha256", "mutant_sha256", "reason"}
        if set(entry) != expected_fields:
            raise MutationReportError(
                f"equivalent entry must contain exactly {sorted(expected_fields)}: {mutant_id}"
            )
        reason = entry["reason"]
        if not isinstance(reason, str) or len(reason.strip()) < 20:
            raise MutationReportError(
                f"equivalent entry needs a meaningful reason of at least 20 characters: {mutant_id}"
            )
        relative_source, function_name = _module_file(mutant_id)
        source_path = source_root / relative_source
        generated_path = mutants_dir / relative_source
        try:
            actual_source_hash = _sha256(source_path.read_bytes())
        except OSError as exc:
            raise MutationReportError(f"cannot read application source {source_path}: {exc}") from exc
        actual_mutant_hash = _mutant_function_hash(generated_path, function_name)
        for field, actual in (
            ("source_sha256", actual_source_hash),
            ("mutant_sha256", actual_mutant_hash),
        ):
            supplied = entry[field]
            if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
                raise MutationReportError(f"invalid {field} for equivalent mutant {mutant_id}")
            if supplied != actual:
                raise MutationReportError(f"{field} mismatch for equivalent mutant {mutant_id}")
        accepted.add(mutant_id)
    return accepted


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


def check_report_file(
    report_path: Path,
    mutants_dir: Path | None = None,
    equivalents_path: Path | None = None,
    source_root: Path | None = None,
) -> dict[str, int]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MutationReportError(f"cannot read mutation report {report_path}: {exc}") from exc

    if mutants_dir is not None:
        complete = complete_raw_report(report, mutants_dir)
    else:
        complete = report
    equivalent_ids: set[str] = set()
    if equivalents_path is not None:
        if mutants_dir is None:
            raise MutationReportError("--equivalents requires --mutants-dir")
        statuses = load_mutant_exit_codes(mutants_dir)
        equivalent_ids = validate_equivalents(
            equivalents_path, mutants_dir, source_root or Path.cwd(), statuses
        )
        if len(equivalent_ids) != complete.get("survived"):
            raise MutationReportError(
                "equivalents file must account for every survivor "
                f"(listed {len(equivalent_ids)}, survived {complete.get('survived')})"
            )
    return validate_report(complete, equivalent_count=len(equivalent_ids))


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
    parser.add_argument(
        "--equivalents", type=Path,
        help="reviewed hash-pinned exact equivalents JSON (optional)",
    )
    parser.add_argument(
        "--source-root", type=Path, default=Path.cwd(),
        help="project source root containing app/ (default: current directory)",
    )
    args = parser.parse_args(argv)
    try:
        stats = check_report_file(
            args.report, args.mutants_dir, args.equivalents, args.source_root
        )
    except MutationReportError as exc:
        print(f"mutation gate failed: {exc}", file=sys.stderr)
        return 1
    suffix = f"; {stats['survived']} reviewed equivalents" if stats["survived"] else ""
    print(
        f"mutation gate passed: {stats['killed']}/{stats['total']} mutants killed"
        f"{suffix} (equivalents remain survivors in score)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
