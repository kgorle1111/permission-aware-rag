"""Run a fixture against a local adapter factory; nonzero exit means gate failure."""

import argparse
import hashlib
import importlib.util
import json
import platform
import subprocess
from copy import deepcopy
from pathlib import Path

from .harness import run_suite
from .reference_adapter import ReferenceAdapter

HERE = Path(__file__).resolve().parent


def load_factory(path: Path):
    spec = importlib.util.spec_from_file_location("leak_kit_adapter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.factory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, help="trusted Python file exporting factory()")
    parser.add_argument("--fixture", type=Path, default=HERE / "controls.json")
    parser.add_argument("--mutants", action="store_true", help="run the project's eight faulty BM25 adapters")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raw = args.fixture.read_bytes()
    fixture = json.loads(raw)
    manifest_path = args.fixture.with_suffix(".manifest.json")
    if manifest_path.exists():
        expected = json.loads(manifest_path.read_text())["fixture_sha256"]
        if hashlib.sha256(raw).hexdigest() != expected:
            parser.error("fixture digest changed; review input/provenance before updating manifest")
    factory = load_factory(args.adapter) if args.adapter else ReferenceAdapter
    report = run_suite(factory, fixture)
    report.update(
        {
            "fixture": fixture["name"],
            "fixture_sha256": hashlib.sha256(raw).hexdigest(),
            "chunks": len(fixture["chunks"]),
            "principal_scopes": len(fixture["principals"]),
            "python": platform.python_version(),
            "adapter": str(args.adapter) if args.adapter else "ReferenceAdapter",
        }
    )
    implementation = hashlib.sha256()
    for path in sorted(HERE.glob("*.py")) + [
        HERE.parents[1] / "app/permission_rag.py",
        HERE.parents[1] / "app/mutants.py",
    ]:
        implementation.update(path.name.encode() + b"\0" + path.read_bytes())
    if args.adapter:
        implementation.update(args.adapter.read_bytes())
    report["implementation_sha256"] = implementation.hexdigest()
    report["source_commit"] = (
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE, text=True, capture_output=True).stdout.strip()
        or None
    )
    if args.mutants:
        from mutants import MUTANTS, GlobalIdf

        report["mutants"] = {
            cls.__name__: run_suite(lambda cls=cls: ReferenceAdapter(cls), fixture) for cls in MUTANTS
        }
        report["mutants_caught"] = sum(
            result["errors"] == 0
            and any(
                result[k]
                for k in (
                    "visibility_failures",
                    "isolation_failures",
                    "recall_misses",
                    "invalid_acl_failures",
                    "revocation_failures",
                )
            )
            for result in report["mutants"].values()
        )
        report["mutants_total"] = len(MUTANTS)
        report["passed"] = report["passed"] and report["mutants_caught"] == report["mutants_total"]
        flat = deepcopy(fixture)
        for chunk in flat["chunks"]:
            chunk["acl_section"] = ["*"]
            chunk["acl_para"] = ["*"]
        report["score_sidechannel_control"] = run_suite(lambda: ReferenceAdapter(GlobalIdf), flat)
        score_control = report["score_sidechannel_control"]
        report["passed"] = report["passed"] and (
            score_control["errors"] == score_control["visibility_failures"] == 0
            and score_control["isolation_failures"] > 0
        )
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
