"""Ops stats and LLM totals are shared by every process on the host (ROADMAP B12)."""

import contextlib
import datetime as dt
import json
import multiprocessing as mp
import pathlib
import sqlite3
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import obs
import pytest
import underwriter_server as srv

srv.rag.audit_path = None  # tests must not write the real audit log


def record_in_child(path: str, outcome: str, count: int, total_ms: float) -> None:
    ops = obs.Ops(5.0, db_path=path)
    for _ in range(count):
        rec = obs.new_record("/ask")
        rec.update(outcome=outcome, total_ms=total_ms)
        ops.record(rec)
    ops.add_llm_totals({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3}, 120.0, 0.25)


def _run_children(path: str) -> None:
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=record_in_child, args=(path, "ok", 3, 100.0)),
        ctx.Process(target=record_in_child, args=(path, "llm_fallback", 2, 900.0)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
        assert p.exitcode == 0


def test_two_processes_record_and_a_third_reads_both(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "ops.sqlite")
    _run_children(path)
    summary = obs.Ops(5.0, db_path=path).summary()
    assert summary["requests"] == 5
    assert {o: v["count"] for o, v in summary["outcomes"].items()} == {"llm_fallback": 2, "ok": 3}
    assert summary["latency_ms"] == {"p50": 100.0, "p95": 900.0}
    assert summary["failure_rate"] == 0.4


def test_llm_totals_add_across_processes(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "ops.sqlite")
    _run_children(path)
    assert obs.Ops(5.0, db_path=path).totals() == {
        "asks": 2,
        "input_tokens": 20,
        "output_tokens": 10,
        "cache_read_input_tokens": 6,
        "est_cost_usd": 0.5,
        "llm_ms": 240.0,
    }


def test_empty_totals_are_zero() -> None:
    assert obs.Ops(5.0).totals() == {
        "asks": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "est_cost_usd": 0.0,
        "llm_ms": 0.0,
    }


def test_the_request_window_is_bounded_in_the_file(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "ops.sqlite"
    ops = obs.Ops(5.0, keep=5, db_path=path)
    for i in range(12):
        rec = obs.new_record("/ask")
        rec.update(outcome="ok", total_ms=float(i))
        ops.record(rec)
    assert ops.summary()["requests"] == 5
    with contextlib.closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM request").fetchone() == (5,)
    assert ops.summary()["latency_ms"]["p50"] == 9.0  # the newest five survive: 7..11


def test_the_audit_endpoint_reflects_requests_recorded_by_another_process(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "ops.sqlite")
    clock = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(srv, "OPS", obs.Ops(5.0, clock=lambda: clock, db_path=path))
    monkeypatch.setattr(srv, "JWT_SECRET", None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        _run_children(path)  # five requests and two LLM calls, none of them from this process
        url = f"http://127.0.0.1:{server.server_address[1]}/audit?user=junior"
        with urllib.request.urlopen(url) as r:
            body = json.load(r)
    finally:
        server.shutdown()
        server.server_close()
        t.join()
    assert body["ops"]["requests"] == 5
    assert body["llm_summary"]["asks"] == 2
