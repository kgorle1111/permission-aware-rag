"""Per-request log line, in-process ops stats, and the daily spend cap.

Logs hold ids, counts and timings only. Never put query, answer or document text in
a record: they are PII and the log outlives the audit trail's redaction.
"""

import contextlib
import datetime as dt
import json
import logging
import math
import os
import pathlib
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import Any

log = logging.getLogger("permrag")
HERE = pathlib.Path(__file__).resolve().parent

# Outcomes that never reached retrieval/generation; excluded from latency percentiles.
REJECTED = {"bad_request", "rate_limited"}
FAILURES = {"failed_closed", "llm_fallback", "degraded"}


def new_record(route: str) -> dict[str, Any]:
    return {
        "request_id": uuid.uuid4().hex,
        "route": route,
        "status": 0,
        "outcome": "",
        "retrieve_ms": 0.0,
        "llm_ms": 0.0,
        "total_ms": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cached": 0,
        "est_cost_usd": 0.0,
        "returned": 0,
        "denied": 0,
        "unverified_citations": 0,
        "uncited_claims": 0,
        "error": None,  # exception class name only, never its message
    }


def emit(rec: dict[str, Any]) -> None:
    log.info(json.dumps(rec, sort_keys=True))


def percentile(values: list[float], pct: int) -> float | None:
    """Nearest-rank: the smallest value with at least pct% of the sample at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(pct / 100 * len(ordered))) - 1]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def ops_db_path(env: Mapping[str, str] = os.environ) -> pathlib.Path:
    """OPS_DB_PATH: the SQLite file every server process on this host shares (default app/ops.sqlite)."""
    return pathlib.Path(env.get("OPS_DB_PATH") or HERE / "ops.sqlite")


# A reservation whose holder died (kill -9, power loss) frees its hold after this long. It must
# exceed the LLM call timeout (60 s) or a slow call could lose its hold and the cap could be passed.
RESERVATION_TTL_S = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (day TEXT PRIMARY KEY, usd REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reservation (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, usd REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS request (
    id INTEGER PRIMARY KEY AUTOINCREMENT, outcome TEXT NOT NULL, total_ms REAL NOT NULL);
CREATE TABLE IF NOT EXISTS llm_totals (
    id INTEGER PRIMARY KEY CHECK (id = 1), asks INTEGER NOT NULL, input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL, cache_read_input_tokens INTEGER NOT NULL,
    est_cost_usd REAL NOT NULL, llm_ms REAL NOT NULL);
INSERT OR IGNORE INTO llm_totals VALUES (1, 0, 0, 0, 0, 0.0, 0.0);
"""

TOTAL_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens")


class Ops:
    """Ops stats, LLM totals and the daily spend cap.

    The cap is a reservation: reserve() atomically holds the projected worst case (or refuses),
    settle() swaps the hold for the actual cost, release() drops it. State lives in a SQLite file
    (WAL, BEGIN IMMEDIATE) so every server process on one host shares one budget and one set of
    stats. The request window keeps the newest `keep` rows. Without db_path the store is private to
    this object, which is what tests and library callers want.
    """

    # kn: one host only (SQLite file locking); a shared DB or Redis once the server runs on several hosts
    def __init__(
        self,
        budget_usd: float,
        clock: Callable[[], dt.datetime] = utc_now,
        keep: int = 10_000,
        db_path: str | os.PathLike[str] | None = None,
    ):
        if budget_usd < 0:
            raise ValueError("DAILY_BUDGET_USD must be >= 0")
        self.budget_usd, self.clock = budget_usd, clock
        self._keep = keep
        self._lock = threading.Lock()
        self._path = str(db_path) if db_path is not None else None
        self._mem = self._connect(":memory:") if self._path is None else None
        if self._mem is not None:
            self._mem.executescript(_SCHEMA)
        self._ready = self._mem is not None

    @staticmethod
    def _connect(target: str) -> sqlite3.Connection:
        # isolation_level=None: transactions are explicit (BEGIN IMMEDIATE) so the write lock is taken up front
        return sqlite3.connect(target, timeout=30, isolation_level=None, check_same_thread=False)

    @staticmethod
    def _init_file(con: sqlite3.Connection) -> None:
        # Switching to WAL needs an exclusive lock and SQLite does not run the busy handler for it,
        # so first-start races between workers must be retried here. The mode is persistent in the file.
        deadline = time.monotonic() + 30
        while True:
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_SCHEMA)
                return
            except sqlite3.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction, serialized across threads (lock) and processes (SQLite write lock)."""
        with self._lock:
            con = self._mem if self._mem is not None else self._connect(self._path or "")
            try:
                if not self._ready:
                    self._init_file(con)
                    self._ready = True
                con.execute("BEGIN IMMEDIATE")
                try:
                    yield con
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
                con.execute("COMMIT")
            finally:
                if con is not self._mem:
                    con.close()

    def _now(self) -> dt.datetime:
        return self.clock().astimezone(dt.UTC)

    def _day(self) -> str:
        return self._now().date().isoformat()

    def spent_today(self) -> float:
        with self._tx() as con:
            return self._spent(con, self._day())

    @staticmethod
    def _spent(con: sqlite3.Connection, day: str) -> float:
        row = con.execute("SELECT usd FROM spend WHERE day = ?", (day,)).fetchone()
        return float(row[0]) if row else 0.0

    def reserve(self, projected_usd: float) -> int | None:
        """Hold `projected_usd` against today's cap; None when spent + held + projected would pass it."""
        now, day = self._now(), self._day()
        with self._tx() as con:
            con.execute("DELETE FROM reservation WHERE expires < ?", (now.timestamp(),))
            held = con.execute(
                "SELECT COALESCE(SUM(usd), 0) FROM reservation WHERE day = ?", (day,)
            ).fetchone()[0]
            if self._spent(con, day) + held + projected_usd > self.budget_usd:
                return None
            cur = con.execute(
                "INSERT INTO reservation (day, usd, expires) VALUES (?, ?, ?)",
                (day, projected_usd, now.timestamp() + RESERVATION_TTL_S),
            )
            return cur.lastrowid

    def settle(self, token: int, actual_usd: float) -> None:
        """Swap the hold for the cost actually incurred. Books the spend even if the hold expired."""
        with self._tx() as con:
            row = con.execute("SELECT day FROM reservation WHERE id = ?", (token,)).fetchone()
            con.execute("DELETE FROM reservation WHERE id = ?", (token,))
            day = row[0] if row else self._day()
            con.execute(
                "INSERT INTO spend (day, usd) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET usd = round(usd + excluded.usd, 6)",
                (day, round(actual_usd, 6)),
            )

    def release(self, token: int) -> None:
        """Drop a hold whose call produced no billable usage (failure, no API key)."""
        with self._tx() as con:
            con.execute("DELETE FROM reservation WHERE id = ?", (token,))

    def record(self, rec: dict[str, Any]) -> None:
        """Stats only; spend is booked by settle() so the cap and the books cannot drift apart."""
        with self._tx() as con:
            con.execute(
                "INSERT INTO request (outcome, total_ms) VALUES (?, ?)", (rec["outcome"], rec["total_ms"])
            )
            con.execute("DELETE FROM request WHERE id <= (SELECT MAX(id) FROM request) - ?", (self._keep,))

    def add_llm_totals(self, usage: Mapping[str, int], llm_ms: float, cost_usd: float) -> None:
        """Running value-receipt totals for /audit: one answered LLM call."""
        with self._tx() as con:
            con.execute(
                "UPDATE llm_totals SET asks = asks + 1, input_tokens = input_tokens + ?, "
                "output_tokens = output_tokens + ?, cache_read_input_tokens = cache_read_input_tokens + ?, "
                "est_cost_usd = round(est_cost_usd + ?, 6), llm_ms = round(llm_ms + ?, 1)",
                (*(usage.get(k, 0) for k in TOTAL_KEYS), cost_usd, llm_ms),
            )

    def totals(self) -> dict[str, Any]:
        with self._tx() as con:
            cur = con.execute(
                "SELECT asks, input_tokens, output_tokens, cache_read_input_tokens, est_cost_usd, llm_ms FROM llm_totals"
            )
            return dict(zip(("asks", *TOTAL_KEYS, "est_cost_usd", "llm_ms"), cur.fetchone(), strict=True))

    def summary(self) -> dict[str, Any]:
        with self._tx() as con:
            recent = [
                {"outcome": o, "total_ms": ms}
                for o, ms in con.execute("SELECT outcome, total_ms FROM request")
            ]
            spend = {day: usd for day, usd in con.execute("SELECT day, usd FROM spend")}
        n = len(recent)
        counts: dict[str, int] = {}
        for r in recent:
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        served = [r["total_ms"] for r in recent if r["outcome"] not in REJECTED]
        failed = sum(c for o, c in counts.items() if o in FAILURES)
        return {
            "requests": n,
            "latency_ms": {"p50": percentile(served, 50), "p95": percentile(served, 95)},
            "cost_per_day_usd": dict(sorted(spend.items())),
            "outcomes": {o: {"count": c, "rate": round(c / n, 4)} for o, c in sorted(counts.items())},
            "failure_rate": round(failed / n, 4) if n else 0.0,
        }
