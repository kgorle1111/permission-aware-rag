"""Runtime hardening: spend-cap reservations under concurrency, readiness covers the vector store,
request ids on every permrag log record. Timeouts and the truthful `source` are pinned next to the
tests they changed (test_cli_and_adapters, test_coverage_security, test_drive_*, test_observability,
test_safety)."""
import datetime as dt
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from app import config, generation, main, observability, resilience, retrieval, vectorstore
from app.identity import Principal

from conftest import auth

DAY = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)


def _ops(budget):
    return observability.Ops(budget, clock=lambda: DAY)


# --- spend cap: reserve / settle ------------------------------------------------------------------


def test_concurrent_reservations_admit_exactly_what_the_cap_fits():
    ops, n, fits = _ops(1.0), 40, 3  # 0.3 each: 3 fit under 1.0, a 4th would make 1.2
    gate, tokens = threading.Barrier(n), []

    def worker():
        gate.wait()
        tokens.append(ops.reserve(0.3))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    granted = [t for t in tokens if t is not None]
    assert len(tokens) == n and len(granted) == fits and len(set(granted)) == fits


def test_hold_counts_until_settled_then_only_actual_cost_counts():
    ops = _ops(1.0)
    held = ops.reserve(0.6)
    assert ops.spent_today() == 0.0
    assert ops.reserve(0.5) is None  # 0.6 held + 0.5 > 1.0, though nothing is spent yet
    ops.settle(held, 0.1)
    assert ops.spent_today() == 0.1
    again = ops.reserve(0.9)  # 0.1 spent + 0.9 == cap: allowed
    assert again is not None and again != held
    ops.settle(again, 0.0)  # a failed call books nothing: the hold is simply released
    assert ops.spent_today() == 0.1
    assert ops.reserve(0.9) is not None


def test_settle_books_on_the_day_the_hold_was_taken():
    clock = {"now": dt.datetime(2026, 10, 10, 23, 59, 59, tzinfo=dt.timezone.utc)}
    ops = observability.Ops(1.0, clock=lambda: clock["now"])
    held = ops.reserve(0.5)
    clock["now"] = dt.datetime(2026, 10, 11, 0, 0, 1, tzinfo=dt.timezone.utc)
    assert ops.reserve(1.0) is not None  # yesterday's hold does not count against today
    ops.settle(held, 0.25)
    assert ops.summary()["cost_per_day_usd"] == {"2026-10-10": 0.25}


def test_concurrent_queries_cannot_all_pass_the_cap(client, monkeypatch):
    """The old check-then-act let every in-flight request through before any of them spent."""
    n, fits = 6, 2
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(generation, "projected_cost", lambda q, r: 0.4)
    monkeypatch.setattr(observability, "OPS", _ops(1.0))  # 2 x 0.4 fit, a 3rd would be 1.2
    resilience.reset_breakers()
    release, calls, lock = threading.Event(), [], threading.Lock()

    def slow_model(query, results):
        with lock:
            calls.append(query)
        assert release.wait(10)
        return generation._generated("generated [handbook]", usage={"output_tokens": 0})

    monkeypatch.setattr(retrieval, "answer", slow_model)
    out = [None] * n

    def ask(i):
        out[i] = retrieval.retrieve(f"vacation policy {i}", Principal("g@x.com"))

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # every request is either parked in the model or answered
        with lock:
            parked = len(calls)
        if parked + sum(o is not None for o in out) == n:
            break
        time.sleep(0.01)
    release.set()
    for t in threads:
        t.join(10)
    resilience.reset_breakers()
    assert len(calls) == fits
    assert sorted(o["source"] for o in out) == ["full_rag"] * fits + ["retrieval_only"] * (n - fits)
    assert sum(o.get("note") == retrieval.BUDGET_NOTE for o in out) == n - fits
    assert observability.OPS._held == {}


def test_generation_failure_releases_the_hold(client, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(generation, "projected_cost", lambda q, r: 0.4)
    monkeypatch.setattr(observability, "OPS", _ops(1.0))
    resilience.reset_breakers()
    monkeypatch.setattr(retrieval, "answer", Mock(side_effect=RuntimeError("provider down")))
    try:
        for i in range(2):
            assert retrieval.retrieve(f"vacation {i}", Principal("g@x.com"))["source"] == "retrieval_only"
        assert observability.OPS._held == {} and observability.OPS.spent_today() == 0.0
        assert observability.OPS.reserve(1.0) is not None  # nothing leaked: the whole cap is free
    finally:
        resilience.reset_breakers()


def test_successful_generation_books_its_actual_cost(client, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(observability, "OPS", _ops(1.0))
    usage = {"input_tokens": 1000, "output_tokens": 100}
    monkeypatch.setattr(retrieval, "answer",
                        lambda q, r: generation._generated("ok [handbook]", usage=usage))
    retrieval.retrieve("vacation policy", Principal("g@x.com"))
    assert observability.OPS.spent_today() == observability.est_cost(usage) == 0.0015
    assert observability.OPS._held == {}


# --- readiness covers the vector store ------------------------------------------------------------


@pytest.mark.parametrize("broken", [
    Mock(side_effect=RuntimeError("qdrant unreachable")),
    Mock(return_value=False),  # reachable, but the collection is gone
])
def test_readyz_is_503_when_the_vector_store_is_not_usable(client, monkeypatch, broken, caplog):
    assert client.get("/readyz").json() == {"ready": True}
    fake = Mock()
    fake.collection_exists = broken
    monkeypatch.setattr(vectorstore, "client", lambda: fake)
    caplog.set_level(logging.ERROR, logger="permrag")
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"detail": "permission reconciliation required"}  # same body as before
    fake.collection_exists.assert_called_once_with(config.COLLECTION)
    expected = ("readiness check failed" if broken.side_effect
                else f"not ready: vector collection {config.COLLECTION!r} missing")
    assert [rec.getMessage() for rec in caplog.records if rec.name == "permrag"] == [expected]


def test_vectorstore_ready_reports_the_real_collection(client, monkeypatch):
    assert vectorstore.ready() is True
    monkeypatch.setattr(config, "COLLECTION", "no-such-collection")
    assert vectorstore.ready() is False


# --- request id on every permrag record -----------------------------------------------------------


class _Own(logging.Handler):
    """Attached to the permrag logger itself: it sees a record before any root handler (and any
    filter on one) does, so only the logger's own filter can have stamped it."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def emit(self, record):
        self.seen.append((record.getMessage(), getattr(record, "request_id", None),
                          record.exc_info is not None))


@pytest.fixture
def own():
    handler, logger = _Own(), logging.getLogger("permrag")
    logger.addHandler(handler)
    yield handler.seen
    logger.removeHandler(handler)


def test_exception_inside_a_request_logs_that_requests_id(client, monkeypatch, own):
    monkeypatch.setattr(retrieval, "SessionLocal", Mock(side_effect=RuntimeError("db down")))
    r = client.post("/query", json={"query": "vacation policy"}, headers=auth("guest"))
    assert r.status_code == 503
    rid = r.headers["X-Request-ID"]
    assert ("retrieval failed closed", rid, True) in own  # the traceback is correlatable
    assert {seen_id for _msg, seen_id, _exc in own} == {rid}


def test_records_outside_a_request_carry_a_placeholder(own):
    logging.getLogger("permrag").warning("background work")
    assert own == [("background work", "-", False)]


def test_served_log_format_names_the_request_id_for_every_logger():
    """In a fresh process (as under uvicorn) the root handler prints the id, and a dependency's
    record, which never passed the permrag filter, still formats instead of raising."""
    env = {**os.environ, "SYNC_INTERVAL_S": "0"}
    code = ("import logging, app.main; logging.getLogger('dep').warning('dep line'); "
            "logging.getLogger('permrag').warning('own line')")
    proc = subprocess.run([sys.executable, "-c", code], cwd=Path(main.__file__).parent.parent,
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "Logging error" not in proc.stderr
    assert "dep WARNING [-] dep line" in proc.stderr
    assert "permrag WARNING [-] own line" in proc.stderr
