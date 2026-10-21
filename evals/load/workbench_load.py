"""Closed-loop load on the demo workbench (GET /query); stdlib only.

    python3 evals/load/workbench_load.py http://127.0.0.1:18521 10 20   # base url, VUs, seconds

Each query gets a random suffix so the answer cache never hits. Errors = non-200 or no "results" key.
Results: evals/results/2026-10-19-load-ceiling/workbench-nano.md.
"""

import concurrent.futures as cf
import http.client
import json
import random
import sys
import time
import urllib.parse

ROLES = ["junior", "senior", "compliance", "auditor"]
QS = ["claims history policy 10023", "loss runs for the marine account", "reinsurance treaty terms", "policy"]


def vu(host: str, port: int, secs: float, seed: int) -> tuple[list[float], int, int]:
    rnd, lat, err, n = random.Random(seed), [], 0, 0
    conn = http.client.HTTPConnection(host, port, timeout=30)
    end = time.monotonic() + secs
    while time.monotonic() < end:
        q = urllib.parse.urlencode({"user": rnd.choice(ROLES), "q": f"{rnd.choice(QS)} {rnd.random():.6f}"})
        t = time.perf_counter()
        try:
            conn.request("GET", f"/query?{q}")
            r = conn.getresponse()
            ok = r.status == 200 and "results" in json.loads(r.read())
        except (OSError, http.client.HTTPException, ValueError):
            ok = False
            conn.close()
            conn = http.client.HTTPConnection(host, port, timeout=30)
        lat.append(time.perf_counter() - t)
        n += 1
        err += not ok
    return lat, err, n


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    base, vus, secs = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    u = urllib.parse.urlparse(base)
    if not u.hostname or not u.port:
        raise SystemExit("base url needs a host and port, e.g. http://127.0.0.1:18521")
    with cf.ThreadPoolExecutor(vus) as ex:
        out = list(ex.map(lambda i: vu(u.hostname or "", u.port or 0, secs, i), range(vus)))
    lat = sorted(x for o in out for x in o[0])
    n, err = sum(o[2] for o in out), sum(o[1] for o in out)

    def pct(p: float) -> float:
        return round(lat[int(p * (len(lat) - 1))] * 1000, 1)

    print(
        json.dumps(
            {
                "vus": vus,
                "n": n,
                "rps": round(n / secs, 1),
                "p50_ms": pct(0.5),
                "p95_ms": pct(0.95),
                "p99_ms": pct(0.99),
                "errors": err,
                "error_rate": round(err / n, 4) if n else None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
