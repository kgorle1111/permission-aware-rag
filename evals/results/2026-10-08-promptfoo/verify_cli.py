"""Verify local promptfoo CLI integration, including two expected negative runs.

Supply an installed promptfoo JS entrypoint. This script installs nothing and
uses no model credentials. JSON exports and logs stay in the chosen output dir.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True, type=Path)
    parser.add_argument("--node", default="node")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cli = args.cli.resolve()
    env = {
        "PATH": os.environ["PATH"],
        "FORCE_COLOR": "0",
        "PROMPTFOO_DISABLE_TELEMETRY": "1",
        "PROMPTFOO_DISABLE_UPDATE": "1",
        "PROMPTFOO_DISABLE_REMOTE_GENERATION": "true",
        "PROMPTFOO_DISABLE_SHARING": "1",
        "PROMPTFOO_SELF_HOSTED": "1",
        "PROMPTFOO_CONFIG_DIR": str(output_dir / "state"),
        "PROMPTFOO_CACHE_PATH": str(output_dir / "cache"),
        "PROMPTFOO_LOG_DIR": str(output_dir / "logs"),
    }
    cases = [
        ("supplied-controls", ROOT / "evals/kit/promptfooconfig.yaml", 0),
        ("public-reference", HERE / "public-reference.yaml", 0),
        ("public-example", HERE / "public-example.yaml", 0),
        ("faulty-retriever", HERE / "faulty-retriever.yaml", 100),
        ("subprocess-failure", HERE / "subprocess-failure.yaml", 100),
    ]
    observations = []
    for name, config, expected_exit in cases:
        destination = output_dir / (name + ".json")
        command = [
            args.node,
            str(cli),
            "eval",
            "-c",
            str(config),
            "--no-cache",
            "--no-write",
            "--no-share",
            "--no-progress-bar",
            "--no-table",
            "-o",
            str(destination),
        ]
        result = subprocess.run(command, cwd=output_dir, env=env, text=True, capture_output=True, timeout=180)
        (output_dir / (name + ".stdout")).write_text(result.stdout)
        (output_dir / (name + ".stderr")).write_text(result.stderr)
        exported = json.loads(destination.read_text())
        stats = exported["results"]["stats"]
        row = exported["results"]["results"][0]
        response = row["response"]
        if name == "subprocess-failure":
            observed = stats["errors"] == 1 and not row["success"] and bool(response.get("error"))
        elif name == "faulty-retriever":
            body = response["output"]
            observed = (
                stats["failures"] == 1
                and not row["success"]
                and row["gradingResult"]["pass"] is False
                and body["passed"] is False
                and body["visibility_failures"] > 0
                and body["isolation_failures"] > 0
            )
        else:
            observed = (
                stats["successes"] == 1
                and row["success"]
                and row["gradingResult"]["pass"] is True
                and response["output"]["passed"] is True
            )
        passed = result.returncode == expected_exit and observed
        record = {
            "case": name,
            "exit_code": result.returncode,
            "expected_exit": expected_exit,
            "expectation_met": passed,
            "successes": stats["successes"],
            "failures": stats["failures"],
            "errors": stats["errors"],
        }
        observations.append(record)
        print(json.dumps(record), flush=True)
        (output_dir / "verification.json").write_text(json.dumps(observations, indent=2) + "\n")
    return 0 if all(row["expectation_met"] for row in observations) else 1


if __name__ == "__main__":
    raise SystemExit(main())
