"""Fail on any vulnerability in a pip-audit JSON report unless a dated package exception covers it.

    uvx pip-audit==2.10.1 -r platform/requirements-models.txt -f json -o report.json || true
    python3 .github/scripts/audit_with_exceptions.py report.json .github/pip-audit-exceptions.json

An exception covers (package, installed version < below_version) until its expiry date; an expired
exception fails the run even if nothing would match it, so it gets re-checked rather than forgotten.
"""

import datetime
import json
import sys


def version_key(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def check(report: dict, exceptions: list[dict], today: datetime.date) -> list[str]:
    errors = [
        f"exception for {e['package']} expired {e['expires']}: re-check upstream, then renew or remove it"
        for e in exceptions
        if datetime.date.fromisoformat(e["expires"]) < today
    ]
    live = [e for e in exceptions if datetime.date.fromisoformat(e["expires"]) >= today]
    for dep in report["dependencies"]:
        for vuln in dep.get("vulns", []):
            covered = any(
                e["package"].lower() == dep["name"].lower()
                and version_key(dep["version"]) < version_key(e["below_version"])
                for e in live
            )
            if not covered:
                errors.append(
                    f"{dep['name']} {dep['version']} {vuln['id']} (fix: {vuln.get('fix_versions')})"
                )
    return errors


def main() -> int:
    report = json.load(open(sys.argv[1]))
    exceptions = json.load(open(sys.argv[2]))["package_exceptions"]
    errors = check(report, exceptions, datetime.date.today())
    ignored = sum(len(d.get("vulns", [])) for d in report["dependencies"]) - sum(
        1 for e in errors if "expired" not in e
    )
    for e in errors:
        print(f"::error::{e}")
    print(f"{len(errors)} blocking, {ignored} covered by dated exceptions")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
