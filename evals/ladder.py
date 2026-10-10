"""Retrieval ladder harness (ROADMAP 7.4): compare rungs, reject any rung that leaks.

A rung is a name plus `factory(docs) -> adapter` (an `evals/kit` adapter built over
`docs`, kit chunk dicts) plus `cls`, the `PermissionRAG`-compatible class behind it,
which feeds the app-corpus gates in `app/run_evals.py`. Per rung:
  (a) kit leak suite (`run_suite`) on controls.json;
  (b) isolation: `run_evals.isolation_gate`, full corpus == each role's readable corpus (ids and scores);
  (c) recall@k on app/evals.json (and hand-labeled leaks);
  (d) latency p50/p95 over the eval queries.
Any leak, isolation diff, suite failure or error marks the rung REJECTED regardless of quality.

    python3 evals/ladder.py           # table: baseline + every mutant as a negative control
    python3 evals/ladder.py --check   # exit 1 if baseline is rejected or any control is accepted
"""

import json
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "app")]

import run_evals  # noqa: E402
import underwriter_server as srv  # noqa: E402
from mutants import MUTANTS  # noqa: E402
from permission_rag import PermissionRAG  # noqa: E402

from evals.kit.harness import run_suite  # noqa: E402
from evals.kit.reference_adapter import ReferenceAdapter  # noqa: E402

FIXTURE = json.loads((ROOT / "evals/kit/controls.json").read_text())
REPEATS = 5  # latency samples per query


@dataclass
class Rung:
    name: str
    factory: Callable[[list], object]
    cls: type
    control: bool = False  # negative control: expected to be REJECTED


def rung_from_class(cls: type, control: bool = False) -> Rung:
    def factory(docs: list):
        adapter = ReferenceAdapter(cls)
        adapter.build(docs)
        return adapter

    return Rung(cls.__name__, factory, cls, control)


def default_rungs() -> list[Rung]:
    baseline = Rung("PermissionRAG (baseline)", rung_from_class(PermissionRAG).factory, PermissionRAG)
    return [baseline] + [rung_from_class(m, control=True) for m in MUTANTS]


def evaluate(rung: Rung, fixture: dict = FIXTURE) -> dict:
    suite = run_suite(lambda: rung.factory([]), fixture)
    iso_diffs, iso_probes = run_evals.isolation_gate(rung.cls, verbose=False)
    rag = run_evals._build(rung.cls, srv.CORPUS)
    hits, total, label_leaks = run_evals.label_gate(rag, verbose=False)
    times = []
    for _ in range(REPEATS):
        for case in run_evals.CASES:
            t0 = time.perf_counter()
            rag.retrieve(case["q"], srv.USERS[case["role"]], k=run_evals.K)
            times.append((time.perf_counter() - t0) * 1000)
    qs = statistics.quantiles(times, n=100, method="inclusive")
    leaks = suite["visibility_failures"] + suite["invalid_acl_failures"] + suite["revocation_failures"]
    isolation = suite["isolation_failures"] + iso_diffs
    reasons = [
        f"{n} {label}"
        for n, label in (
            (leaks + label_leaks, "leaks"),
            (isolation, "isolation diffs"),
            (suite["recall_misses"], "suite recall misses"),
            (suite["errors"], "errors"),
        )
        if n
    ]
    return {
        "name": rung.name,
        "control": rung.control,
        "leaks": leaks + label_leaks,
        "isolation_diffs": isolation,
        "isolation_probes": iso_probes + suite["probes"],
        "recall_hits": hits,
        "recall_total": total,
        "p50_ms": qs[49],
        "p95_ms": qs[94],
        "rejected": bool(reasons) or not suite["passed"],
        "reasons": reasons or (["suite failed"] if not suite["passed"] else []),
    }


def table(results: list[dict]) -> str:
    base = results[0]
    lines = [
        f"| rung | verdict | leaks | isolation diffs | recall@{run_evals.K} | p50 ms | p95 ms | why rejected |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        delta = "baseline" if r is base else f"{r['recall_hits'] - base['recall_hits']:+d} vs baseline"
        lines.append(
            f"| {r['name']} | {'REJECTED' if r['rejected'] else 'accepted'} | {r['leaks']} "
            f"| {r['isolation_diffs']}/{r['isolation_probes']} | {r['recall_hits']}/{r['recall_total']} ({delta}) "
            f"| {r['p50_ms']:.2f} | {r['p95_ms']:.2f} | {', '.join(r['reasons']) or '-'} |"
        )
    return "\n".join(lines)


def check(results: list[dict]) -> list[str]:
    problems = [f"baseline rejected: {', '.join(results[0]['reasons'])}"] if results[0]["rejected"] else []
    problems += [
        f"negative control accepted: {r['name']}" for r in results[1:] if r["control"] and not r["rejected"]
    ]
    return problems


def main(argv: list[str]) -> int:
    results = [evaluate(r) for r in default_rungs()]
    print(table(results))
    problems = check(results)
    for p in problems:
        print(f"FAIL: {p}")
    return 1 if problems and "--check" in argv else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
