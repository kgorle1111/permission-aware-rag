#!/usr/bin/env python3
"""Reproduce four preregistered faults in decorated platform security code.

This is a separate verifier authored after the independent contract tests were
frozen. It inspects source and existing tests; it does not modify either.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

CASES = [
    {
        "id": "principal-universal-hr",
        "path": "app/identity.py",
        "source_sha256": "9d7f2496c79c76f93ac76ef70a111669f2fd5b0215f0a1ac04d7486d59be83f6",
        "old": 'return [f"user:{self.user_id}", *[f"group:{g}" for g in self.groups], "*"]',
        "new": 'return [f"user:{self.user_id}", *[f"group:{g}" for g in self.groups], "group:hr", "*"]',
        "tests": [
            "tests/test_leakage.py::test_exact_content_probes_leak_nothing",
            "tests/test_leakage.py::test_authorized_users_do_get_answers",
        ],
        "expected": "Non-HR caller receives an HR document or canary; authorized control still passes.",
        "failure_markers": ["LEAK:", "hr-salaries"],
    },
    {
        "id": "scope-universal-cache",
        "path": "app/identity.py",
        "source_sha256": "9d7f2496c79c76f93ac76ef70a111669f2fd5b0215f0a1ac04d7486d59be83f6",
        "old": 'return json.dumps(sorted(set(self.principals)), separators=(",", ":"))',
        "new": 'return "shared-principal-scope"',
        "tests": ["tests/test_side_channels.py::test_cache_is_permission_scoped"],
        "expected": "Guest receives the executive canary from a CEO-warmed cache.",
        "failure_markers": ["EXEC-CANARY-9d4e2", "not in"],
    },
    {
        "id": "query-forged-hr-principal",
        "path": "app/main.py",
        "source_sha256": "6b632a566e486739dfced29a741b30bd2a38bd9108291e24ec7a98e77d12795f",
        "old": "return retrieve(body.query, principal, k=body.k or None)",
        "new": 'return retrieve(body.query, Principal(user_id="forged-hr@independent.test", groups=("hr",)), k=body.k or None)',
        "tests": ["tests/test_failclosed.py::test_client_cannot_self_assert_groups_via_request"],
        "expected": "Guest request violates the allowed-public-document assertion after route forges HR.",
        "failure_markers": ['assert all(x["doc_id"]', '"handbook"', '"company-strategy"'],
    },
    {
        "id": "audit-no-security-guard",
        "path": "app/main.py",
        "source_sha256": "6b632a566e486739dfced29a741b30bd2a38bd9108291e24ec7a98e77d12795f",
        "old": '    if "security" not in principal.groups:\n        raise HTTPException(403, "audit access requires group:security")\n',
        "new": "",
        "tests": ["tests/test_failclosed.py::test_audit_endpoint_requires_security_group"],
        "expected": "Non-security audit request returns 200 rather than 403.",
        "failure_markers": ["assert 200 == 403"],
    },
]

TEST_HASHES = {
    "tests/conftest.py": "6a1ff3ca5ebc08fe3646fe9ca64e64a618caec5d5227e53cb4a85b5ed854712a",
    "tests/test_leakage.py": "060dc8445f7f97f812478378cc96d2d9ccf5b130c4eef21e89d34d789b5780aa",
    "tests/test_side_channels.py": "9679c54be448c1f983abfa2ff0590c4359ca24b56718b3154b077998a9403776",
    "tests/test_failclosed.py": "37e151cce04d6db875ed17b81982cd3bf0a1a8250806c0247c23dfff7ef68be6",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def ignore(directory: str, names: list[str]) -> set[str]:
    omitted = shutil.ignore_patterns(
        "mutants",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".coverage*",
        "htmlcov",
        ".venv",
        ".env*",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "*.db",
        "*.db-*",
        "*.sqlite*",
        "qdrant_data",
        "data",
    )(directory, names)
    return set(omitted) | {name for name in names if (Path(directory) / name).is_symlink()}


def prepare(source: Path, workspace: Path, output: Path) -> list[str]:
    baseline = workspace / "baseline"
    shutil.copytree(source, baseline, ignore=ignore)
    for name, expected in TEST_HASHES.items():
        if sha(baseline / name) != expected:
            raise ValueError(f"Selected test/fixture drift: {name}")
    for case in CASES:
        path = baseline / case["path"]
        original = path.read_text()
        if sha(path) != case["source_sha256"] or original.count(case["old"]) != 1:
            raise ValueError(f"Exact source patch drift: {case['id']}")
        mutated = original.replace(case["old"], case["new"])
        ast.parse(mutated)
        target = workspace / case["id"]
        shutil.copytree(baseline, target, ignore=ignore)
        (target / case["path"]).write_text(mutated)
        patch = difflib.unified_diff(
            original.splitlines(True),
            mutated.splitlines(True),
            fromfile="baseline/" + case["path"],
            tofile=case["id"] + "/" + case["path"],
        )
        (output / (case["id"] + ".patch")).write_text("".join(patch))
    return list(dict.fromkeys(test for case in CASES for test in case["tests"]))


def run_tests(workspace: Path, name: str, tests: list[str], output: Path, timeout: float) -> dict:
    runtime = workspace / name / "_test-runtime"
    runtime.mkdir()
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["TMPDIR"] = str(runtime)
    junit = output / (name + ".xml")
    command = [
        sys.executable,
        "-m",
        "pytest",
        *tests,
        "-q",
        "-o",
        "addopts=",
        "--basetemp=" + str(runtime / "pytest"),
        "--junitxml=" + str(junit),
    ]
    try:
        process = subprocess.run(
            command,
            cwd=workspace / name,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        log, code = process.stdout, process.returncode
    except subprocess.TimeoutExpired as exc:
        log = exc.stdout or ""
        if isinstance(log, bytes):
            log = log.decode(errors="replace")
        log += f"\nVerifier timeout after {timeout} seconds.\n"
        code = None
    (output / (name + ".log")).write_text(log)
    records = []
    if junit.exists():
        for node in ET.parse(junit).iter("testcase"):
            records.append(
                {
                    "name": node.get("name"),
                    "failures": [
                        {"message": failure.get("message", ""), "text": failure.text or ""}
                        for failure in node.findall("failure")
                    ],
                    "errors": [error.get("message", "") for error in node.findall("error")],
                    "skipped": len(node.findall("skipped")),
                }
            )
    return {"id": name, "returncode": code, "command": command, "tests": records}


def caught(result: dict, case: dict) -> bool:
    records = result["tests"]
    if (
        result["returncode"] != 1
        or len(records) != len(case["tests"])
        or any(record["errors"] or record["skipped"] for record in records)
    ):
        return False
    target = case["tests"][0].split("::")[-1]
    if {record["name"] for record in records} != {test.split("::")[-1] for test in case["tests"]} or any(
        record["failures"] for record in records if record["name"] != target
    ):
        return False
    for record in records:
        if record["name"] != target:
            continue
        for failure in record["failures"]:
            message = failure["message"].lstrip()
            text = message + "\n" + failure["text"]
            if (message.startswith("AssertionError") or message.startswith("assert ")) and all(
                marker in text for marker in case["failure_markers"]
            ):
                return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New or empty directory for JSON, exact patches, logs and JUnit",
    )
    parser.add_argument("--platform", type=Path, default=Path(__file__).resolve().parents[1] / "platform")
    parser.add_argument(
        "--timeout", type=float, default=120, help="Maximum seconds per pytest invocation (default: 120)"
    )
    args = parser.parse_args()
    output, source = args.output.resolve(), args.platform.resolve()
    if output == source or source in output.parents:
        parser.error("Output must be outside the platform source tree")
    if output.exists() and any(output.iterdir()):
        parser.error("Output directory must be new or empty; existing results are preserved")
    if args.timeout <= 0:
        parser.error("Timeout must be positive")
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "runner_sha256": sha(Path(__file__)),
        "python_version": sys.version,
        "platform": sys.platform,
        "results": [],
        "caught": 0,
        "total": len(CASES),
        "scope": "Four selected supplemental manual faults; not exhaustive or part of the mutmut denominator.",
        "provenance": "Separate verifier after independent test authoring froze; uses source inspection and unchanged existing tests.",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="permrag-decorated-verifier-") as directory:
            workspace = Path(directory)
            tests = prepare(source, workspace, output)
            save(
                output / "preregistration.json",
                {
                    "frozen_at_utc": dt.datetime.now(dt.UTC).isoformat(),
                    "runner_sha256": report["runner_sha256"],
                    "cases": [
                        {
                            **case,
                            "mutated_sha256": sha(workspace / case["id"] / case["path"]),
                            "patch_sha256": sha(output / (case["id"] + ".patch")),
                        }
                        for case in CASES
                    ],
                    "baseline_tests": tests,
                    "selected_test_hashes": TEST_HASHES,
                    "source_manifest": {
                        str(path.relative_to(workspace / "baseline")): sha(path)
                        for path in sorted((workspace / "baseline").rglob("*"))
                        if path.is_file()
                    },
                    "classification": "Code 1 plus the specified security AssertionError, no skipped/setup/error cases; baseline and positive controls pass first.",
                },
            )
            baseline = run_tests(workspace, "baseline", tests, output, args.timeout)
            report["results"].append(baseline)
            if (
                baseline["returncode"] != 0
                or len(baseline["tests"]) != len(tests)
                or any(row["failures"] or row["errors"] or row["skipped"] for row in baseline["tests"])
            ):
                report["status"] = "baseline_failed"
                return 2
            for case in CASES:
                result = run_tests(workspace, case["id"], case["tests"], output, args.timeout)
                result["caught_security_assertion"] = caught(result, case)
                result["status"] = (
                    "caught"
                    if result["caught_security_assertion"]
                    else "survived"
                    if result["returncode"] == 0
                    else "invalid_failure"
                )
                report["results"].append(result)
                report["caught"] += int(result["caught_security_assertion"])
            report["status"] = "passed" if report["caught"] == len(CASES) else "failed"
            return 0 if report["status"] == "passed" else 1
    except (OSError, ValueError, SyntaxError, ET.ParseError) as exc:
        report["status"], report["error"] = "invalid_experiment", str(exc)
        return 2
    finally:
        report["finished_at_utc"] = dt.datetime.now(dt.UTC).isoformat()
        report["temporary_snapshots_cleaned"] = True
        save(output / "result.json", report)
        print(
            f"{report.get('status')}: {report['caught']}/{len(CASES)} manual faults caught; artifacts: {output}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
