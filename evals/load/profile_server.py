"""Run the platform app with per-stage wall-clock timers, WITHOUT editing platform/app.

Wraps the functions retrieval.py calls (by rebinding names in that module) and exposes
GET /__timings (JSON, ?reset=1 clears). Diagnosis tool only; never part of a deployment.

  cd platform && python ../evals/load/profile_server.py 8093
"""

import sys
import threading
import time
from contextlib import contextmanager

import uvicorn

from app import main, retrieval

_lock = threading.Lock()
_t: dict[str, list[float]] = {}


def _rec(name: str, ms: float):
    with _lock:
        _t.setdefault(name, []).append(ms)


def _timed(name, fn):
    def wrapper(*a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            _rec(name, (time.perf_counter() - t0) * 1000)

    return wrapper


_orig_tx = retrieval.permission_transaction


@contextmanager
def _tx():
    t0 = time.perf_counter()
    cm = _orig_tx()
    entered = False
    try:
        with cm as pair:  # entering = open session + the UPDATE that takes the SQLite write lock
            _rec("tx_enter_wait", (time.perf_counter() - t0) * 1000)
            entered = True
            t1 = time.perf_counter()
            try:
                yield pair
            finally:
                _rec("tx_body", (time.perf_counter() - t1) * 1000)
            t2 = time.perf_counter()
        _rec("tx_commit", (time.perf_counter() - t2) * 1000)
    finally:
        if not entered:
            _rec("tx_enter_failed", (time.perf_counter() - t0) * 1000)


retrieval.permission_transaction = _tx
for name in ("embed_one", "search", "search_unfiltered_count", "write_audit", "fingerprint_problem"):
    setattr(retrieval, name, _timed(name, getattr(retrieval, name)))


def _pct(v, p):
    s = sorted(v)
    return s[max(1, -(-p * len(s) // 100)) - 1]


@main.app.get("/__timings")
def timings(reset: int = 0):
    with _lock:
        out = {
            k: {
                "n": len(v),
                "mean": round(sum(v) / len(v), 3),
                "p50": round(_pct(v, 50), 3),
                "p95": round(_pct(v, 95), 3),
                "p99": round(_pct(v, 99), 3),
                "sum_s": round(sum(v) / 1000, 2),
            }
            for k, v in _t.items()
            if v
        }
        if reset:
            _t.clear()
    return out


if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[1]))
