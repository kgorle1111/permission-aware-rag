"""Per-request log line, in-process ops stats and the daily spend cap.

Logs hold ids, counts and timings only. Never put query, answer or document text in a
record: it is PII, and the log outlives the audit table's redaction.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import threading
import uuid
from collections import deque

from . import config

log = logging.getLogger("permrag")


# Haiku 4.5 $/MTok (2026-07): input 1.00, output 5.00, cache read 0.10, cache write 1.25
PRICE = {
    "input_tokens": 1.00,
    "output_tokens": 5.00,
    "cache_read_input_tokens": 0.10,
    "cache_creation_input_tokens": 1.25,
}
# Outcomes that never reached retrieval; excluded from latency percentiles.
REJECTED = {"bad_request"}
FAILURES = {"failed_closed", "llm_fallback", "degraded"}
_CITE = re.compile(r"\[([^\[\]\r\n]+)\]")


def new_record(request_id: str | None = None) -> dict:
    return {
        "request_id": request_id or uuid.uuid4().hex,
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
        "source": "none",
        "degraded": False,
    }


def emit(rec: dict) -> None:
    log.info(json.dumps(rec, sort_keys=True))


def clean_usage(usage: dict) -> dict:
    """Token counts from a provider payload; anything that is not a non-negative int counts as 0."""
    return {k: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0
            for k, v in ((k, usage.get(k, 0)) for k in PRICE)}


def est_cost(usage: dict) -> float:
    clean = clean_usage(usage)
    return round(sum(clean[k] * p for k, p in PRICE.items()) / 1e6, 6)


def unverified_citations(text: str, doc_ids: set[str]) -> int:
    """Distinct [id] citations naming a document that was not retrieved. Redaction
    placeholders look like citations and are not."""
    cited = {c for c in _CITE.findall(text) if not c.startswith("REDACTED:")}
    return len(cited - doc_ids)


def percentile(values: list[float], pct: int) -> float | None:
    """Nearest-rank: the smallest value with at least pct% of the sample at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(pct / 100 * len(ordered))) - 1]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Ops:
    # kn: per-process memory (the audit table has no outcome/latency/cost columns); add them via a migration to span workers or survive restarts
    def __init__(self, budget_usd: float, clock=utc_now, keep: int = 10_000):
        if budget_usd < 0:
            raise ValueError("DAILY_BUDGET_USD must be >= 0")
        self.budget_usd, self.clock = budget_usd, clock
        self._recent: deque[dict] = deque(maxlen=keep)
        self._spend: dict[str, float] = {}
        self._lock = threading.Lock()

    def _day(self) -> str:
        return self.clock().astimezone(dt.timezone.utc).date().isoformat()

    def spent_today(self) -> float:
        with self._lock:
            return self._spend.get(self._day(), 0.0)

    def would_exceed(self, projected_usd: float) -> bool:
        """True when one more call projected at `projected_usd` would pass today's cap."""
        return self.spent_today() + projected_usd > self.budget_usd

    def record(self, rec: dict) -> None:
        day = self._day()
        with self._lock:
            self._recent.append({"outcome": rec["outcome"], "total_ms": rec["total_ms"]})
            self._spend[day] = round(self._spend.get(day, 0.0) + rec["est_cost_usd"], 6)

    def summary(self) -> dict:
        with self._lock:
            recent, spend = list(self._recent), dict(self._spend)
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


OPS = Ops(config.DAILY_BUDGET_USD)
