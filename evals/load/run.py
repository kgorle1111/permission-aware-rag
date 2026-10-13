"""Closed-loop load test for the platform HTTP API (stdlib only: threads + http.client).

  python evals/load/run.py run  --urls http://127.0.0.1:8091 --tokens tokens.json --label 1w-miss --out runs/
  python evals/load/run.py render runs/ --out evals/results/2026-10-12-load-test

Each virtual user (VU) is one persona with one keep-alive connection, sending requests back to back.
Every 200 response is checked against the permission oracle. See evals/load/README.md.
"""

import argparse
import http.client
import json
import os
import platform
import random
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import PERSONAS, QUERIES, Oracle, percentile, principals, wilson_upper  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


class VU(threading.Thread):
    def __init__(self, idx, url, token, persona, oracle, mode, stop_at, warm_until, rng):
        super().__init__(daemon=True)
        self.idx, self.token, self.persona, self.oracle = idx, token, persona, oracle
        self.mode, self.stop_at, self.warm_until, self.rng = mode, stop_at, warm_until, rng
        u = urllib.parse.urlparse(url)
        self.host, self.port = u.hostname, u.port
        self.held = principals(*persona)
        self.samples = []  # (latency_ms, status, source, n_results, request_id, violations)

    def body(self, n):
        if self.mode == "health":
            return None
        q = self.rng.choice(QUERIES)
        # miss: unique suffix defeats the per-process answer cache; hit: fixed pool, cache warm
        return json.dumps({"query": f"{q} ref{self.idx}x{n}" if self.mode == "miss" else q})

    def run(self):
        conn, n = None, 0
        while time.perf_counter() < self.stop_at:
            if conn is None:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=60)
            n += 1
            body = self.body(n)
            t0 = time.perf_counter()
            status, source, results, rid = 0, "", [], ""
            try:
                if body is None:
                    conn.request("GET", "/healthz")
                else:
                    conn.request(
                        "POST",
                        "/query",
                        body,
                        {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                    )
                resp = conn.getresponse()
                raw = resp.read()
                status, rid = resp.status, resp.getheader("X-Request-ID", "")
                if status == 200 and body is not None:
                    data = json.loads(raw)
                    source, results = data.get("source", ""), data.get("results", [])
            except Exception as exc:
                source = type(exc).__name__
                conn.close()
                conn = None
            ms = (time.perf_counter() - t0) * 1000
            if t0 >= self.warm_until:
                viol = self.oracle.violations(results, self.held) if results else []
                self.samples.append((ms, status, source, len(results), rid, viol))
        if conn:
            conn.close()


def run_level(urls, tokens, oracle, vus, mode, duration, warmup, seed):
    start = time.perf_counter()
    warm_until, stop_at = start + warmup, start + warmup + duration
    threads = []
    for i in range(vus):
        persona = PERSONAS[i % len(PERSONAS)]
        threads.append(
            VU(
                i,
                urls[i % len(urls)],
                tokens[persona[0]],
                persona,
                oracle,
                mode,
                stop_at,
                warm_until,
                random.Random(seed * 1000 + i),
            )
        )
    cpu0 = time.process_time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    samples = [s for t in threads for s in t.samples]
    lat = [s[0] for s in samples]
    ok = [s for s in samples if s[1] == 200 and s[2] != "unavailable"]
    leaks = sum(len(s[5]) for s in samples)
    return {
        "vus": vus,
        "requests": len(samples),
        "duration_s": duration,
        "rps": round(len(samples) / duration, 1),
        "p50_ms": percentile(lat, 50),
        "p95_ms": percentile(lat, 95),
        "p99_ms": percentile(lat, 99),
        "errors": len(samples) - len(ok),
        "error_rate": round(1 - len(ok) / max(1, len(samples)), 4),
        "results_returned": sum(s[3] for s in samples),
        "mean_results": round(sum(s[3] for s in samples) / max(1, len(samples)), 2),
        "responses_checked": sum(1 for s in samples if s[3]),
        "leaked_results": leaks,
        "leaks_over_results": f"{leaks}/{sum(s[3] for s in samples)}",
        "leak_rate_wilson95_upper": round(wilson_upper(leaks, sum(s[3] for s in samples)), 6),
        "client_cpu_s": round(time.process_time() - cpu0, 1),
        "request_ids": [s[4] for s in samples if s[4]],
    }


def server_split(log_paths, request_ids):
    """retrieve_ms vs total_ms from the server's own per-request JSON log lines, joined by X-Request-ID."""
    wanted, rows = set(request_ids), []
    for p in log_paths:
        for line in Path(p).read_text(errors="replace").splitlines():
            i = line.find('{"')
            if i < 0:
                continue
            try:
                rec = json.loads(line[i:])
            except ValueError:
                continue
            if rec.get("request_id") in wanted:
                rows.append(rec)
    if not rows:
        return None
    tot = [r["total_ms"] for r in rows]
    ret = [r["retrieve_ms"] for r in rows]
    rest = [r["total_ms"] - r["retrieve_ms"] for r in rows]
    return {
        "n": len(rows),
        "failed_closed": sum(1 for r in rows if r.get("outcome") == "failed_closed"),
        "total_p50": percentile(tot, 50),
        "total_p95": percentile(tot, 95),
        "retrieve_p50": percentile(ret, 50),
        "retrieve_p95": percentile(ret, 95),
        "outside_retrieve_p50": round(percentile(rest, 50), 1),
        "outside_retrieve_p95": round(percentile(rest, 95), 1),
    }


def machine():
    cpu = (
        subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
        ).stdout.strip()
        or platform.processor()
    )
    return {
        "cpu": cpu,
        "cores": os.cpu_count(),
        "os": platform.platform(),
        "client_python": platform.python_version(),
    }


def cmd_run(a):
    tokens = json.loads(Path(a.tokens).read_text())
    oracle = Oracle.from_files(ROOT / "platform/corpus/docs.json", ROOT / "platform/corpus/permissions.jsonl")
    urls = a.urls.split(",")
    levels = []
    for vus in [int(x) for x in a.concurrency.split(",")]:
        r = run_level(urls, tokens, oracle, vus, a.mode, a.duration, a.warmup, a.seed)
        if a.server_logs:
            r["server"] = server_split(a.server_logs.split(","), r["request_ids"])
        del r["request_ids"]
        levels.append(r)
        print(json.dumps(r), flush=True)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.label}.json").write_text(
        json.dumps(
            {
                "label": a.label,
                "mode": a.mode,
                "servers": len(urls),
                "warmup_s": a.warmup,
                "seed": a.seed,
                "levels": levels,
                "machine": machine(),
                "notes": a.notes,
            },
            indent=1,
        )
    )


def cmd_render(a):
    runs = [json.loads(p.read_text()) for p in sorted(Path(a.dir).glob("*.json"))]
    lines = [
        "| config | VUs | req/s | p50 ms | p95 ms | p99 ms | error rate | srv fail-closed | srv total p50 | srv retrieve p50 | "
        "srv outside-retrieve p50 | leaks/results | Wilson95 upper |",
        "|" + "---|" * 13,
    ]
    for run in runs:
        for lv in run["levels"]:
            s = lv.get("server") or {}
            lines.append(
                f"| {run['label']} | {lv['vus']} | {lv['rps']} | {lv['p50_ms']:.1f} | {lv['p95_ms']:.1f} | "
                f"{lv['p99_ms']:.1f} | {lv['error_rate']:.2%} | {s.get('failed_closed', '-')} | {s.get('total_p50', '-')} | "
                f"{s.get('retrieve_p50', '-')} | {s.get('outside_retrieve_p50', '-')} | "
                f"{lv['leaks_over_results']} | {lv['leak_rate_wilson95_upper']:.2e} |"
            )
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "table.md").write_text("\n".join(lines) + "\n")
    config = json.loads(Path(a.config).read_text()) if a.config else {}
    (out / "report.json").write_text(
        json.dumps({"machine": machine(), "config": config, "runs": runs}, indent=1)
    )
    print("\n".join(lines))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--urls", required=True, help="comma list; VUs are assigned round-robin")
    r.add_argument("--tokens", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--mode", choices=["miss", "hit", "health"], default="miss")
    r.add_argument("--concurrency", default="1,5,10,25,50")
    r.add_argument("--duration", type=float, default=60)
    r.add_argument("--warmup", type=float, default=5)
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--server-logs", default="")
    r.add_argument("--notes", default="")
    d = sub.add_parser("render")
    d.add_argument("dir")
    d.add_argument("--out", required=True)
    d.add_argument("--config", help="JSON file describing server config/versions, embedded in report.json")
    args = ap.parse_args()
    cmd_run(args) if args.cmd == "run" else cmd_render(args)
