"""In-process A/B: where does the single-process ceiling come from? (diagnosis only)

Calls retrieval.retrieve() from N threads, no HTTP, so uvicorn/JWT/client overhead is excluded.
  A  baseline          : permission_transaction (SQLite UPDATE lock) + audit INSERT, as shipped
  B  no SQL write lock : same retrieval, transaction opens a plain read session, audit write skipped
                         (a what-if, NOT a safe configuration: it removes the serialization point)

  cd platform && python ../evals/load/inproc_bench.py [seconds] [rounds]
Run it in the same scratch environment as the server (DATABASE_URL, QDRANT_PATH, ...).
"""

import itertools
import statistics
import sys
import threading
import time
from contextlib import contextmanager

from app.identity import Principal

from app import retrieval, store

orig_tx, orig_audit = retrieval.permission_transaction, retrieval.write_audit
counter = itertools.count()


@contextmanager
def read_only_tx():
    with store.SessionLocal() as s:
        yield s, s.get(store.PermissionState, 1)


def hammer(threads: int, seconds: float) -> float:
    stop, done = time.perf_counter() + seconds, [0] * threads
    who = Principal("alice@company.com", ("eng",))

    def work(i):
        while time.perf_counter() < stop:
            retrieval.retrieve(f"on-call rotation pager handoff u{next(counter)}", who)
            done[i] += 1

    ts = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return sum(done) / seconds


def set_variant(name: str):
    if name == "A":
        retrieval.permission_transaction, retrieval.write_audit = orig_tx, orig_audit
    else:
        retrieval.permission_transaction, retrieval.write_audit = read_only_tx, lambda *a, **k: None


if __name__ == "__main__":
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    hammer(4, 2)  # warm-up
    res: dict[tuple[str, int], list[float]] = {}
    for _ in range(rounds):  # interleave variants so host noise hits both
        for variant in "AB":
            for n in (1, 8, 25):
                set_variant(variant)
                res.setdefault((variant, n), []).append(hammer(n, seconds))
    for (variant, n), v in sorted(res.items()):
        print(
            f"variant={variant} threads={n:>2} req/s median={statistics.median(v):7.1f} runs={[round(x) for x in v]}"
        )
