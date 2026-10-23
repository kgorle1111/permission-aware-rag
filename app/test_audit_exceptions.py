"""The CI audit's dated package exceptions cover exactly what they name, and nothing past their expiry."""

import datetime
import importlib.util
import pathlib
from typing import Any

_path = pathlib.Path(__file__).resolve().parent.parent / ".github" / "scripts" / "audit_with_exceptions.py"
_spec = importlib.util.spec_from_file_location("audit_with_exceptions", _path)
assert _spec is not None and _spec.loader is not None
ae = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ae)

TODAY = datetime.date(2026, 10, 23)
PILLOW = {"package": "pillow", "below_version": "13", "expires": "2026-11-20"}


def report(*deps: tuple[str, str, list[str]]) -> dict[str, Any]:
    return {
        "dependencies": [{"name": n, "version": v, "vulns": [{"id": i} for i in ids]} for n, v, ids in deps]
    }


def test_covered_package_below_the_fixed_version_passes() -> None:
    assert ae.check(report(("Pillow", "12.3.0", ["CVE-1", "CVE-2"])), [PILLOW], TODAY) == []


def test_any_other_package_fails() -> None:
    errors = ae.check(report(("pillow", "12.3.0", ["CVE-1"]), ("numpy", "2.0.0", ["CVE-9"])), [PILLOW], TODAY)
    assert len(errors) == 1 and "numpy" in errors[0]


def test_the_same_package_at_or_above_the_cap_fails() -> None:
    assert len(ae.check(report(("pillow", "13.0.0", ["CVE-1"])), [PILLOW], TODAY)) == 1
    assert len(ae.check(report(("pillow", "13.1", ["CVE-1"])), [PILLOW], TODAY)) == 1


def test_an_expired_exception_fails_even_with_nothing_to_cover() -> None:
    expired = {**PILLOW, "expires": "2026-10-22"}
    assert any("expired" in e for e in ae.check(report(), [expired], TODAY))
    assert len(ae.check(report(("pillow", "12.3.0", ["CVE-1"])), [expired], TODAY)) == 2


def test_the_expiry_day_itself_still_covers() -> None:
    assert (
        ae.check(report(("pillow", "12.3.0", ["CVE-1"])), [{**PILLOW, "expires": "2026-10-23"}], TODAY) == []
    )


def test_version_comparison_is_numeric_not_lexical() -> None:
    assert ae.version_key("9.5.0") < ae.version_key("13")
    assert ae.version_key("12.10.0") > ae.version_key("12.9.0")
