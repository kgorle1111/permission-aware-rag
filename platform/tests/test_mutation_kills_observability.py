"""Kills for observability/generation/retrieval mutants that survived the CI gate (mutmut 3.8)."""
import datetime as dt
import json
import logging
import time
from unittest.mock import Mock

import pytest

from app import config, generation, retrieval
from app import observability as obs


def _rec(outcome="ok", total_ms=1.0, cost=0.0):
    return {"outcome": outcome, "total_ms": total_ms, "est_cost_usd": cost}


def _utc(h=12):
    return dt.datetime(2026, 10, 10, h, 0, tzinfo=dt.timezone.utc)


# ---- clean_usage ----------------------------------------------------------------
def test_clean_usage_exact():
    u = {"input_tokens": 0, "output_tokens": -5, "cache_read_input_tokens": True,
         "cache_creation_input_tokens": 2.5}
    assert obs.clean_usage(u) == dict.fromkeys(obs.PRICE, 0)
    ok = {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 1,
          "cache_creation_input_tokens": 9}
    assert obs.clean_usage(ok) == ok
    assert obs.clean_usage({"input_tokens": "x"}) == dict.fromkeys(obs.PRICE, 0)
    assert obs.clean_usage({}) == dict.fromkeys(obs.PRICE, 0)


def test_est_cost_exact_divisor():
    assert obs.est_cost({"input_tokens": 1_000_000}) == 1.0
    assert obs.est_cost({"output_tokens": 3, "cache_read_input_tokens": 10}) == 0.000016


# ---- emit / new_record ----------------------------------------------------------
def test_emit_sorted_keys_byte_exact(caplog):
    rec = {"z": 1, "a": 2, "m": {"y": 1, "b": 2}}
    with caplog.at_level(logging.INFO, logger="permrag"):
        obs.emit(rec)
    assert caplog.records[-1].getMessage() == '{"a": 2, "m": {"b": 2, "y": 1}, "z": 1}'


def test_new_record_exact():
    assert obs.new_record("rid") == {
        "request_id": "rid", "outcome": "", "retrieve_ms": 0.0, "llm_ms": 0.0, "total_ms": 0.0,
        "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "est_cost_usd": 0.0,
        "returned": 0, "denied": 0, "unverified_citations": 0, "source": "none", "degraded": False}
    got = obs.new_record()
    assert len(got["request_id"]) == 32


# ---- percentile / utc_now -------------------------------------------------------
def test_percentile_nearest_rank():
    vals = list(range(1, 102))
    assert obs.percentile(vals, 100) == 101
    assert obs.percentile(vals, 50) == 51
    assert obs.percentile([], 50) is None


def test_utc_now_is_aware_utc():
    assert obs.utc_now().tzinfo is dt.timezone.utc


# ---- Ops ------------------------------------------------------------------------
def test_ops_negative_budget_message():
    with pytest.raises(ValueError) as e:
        obs.Ops(-1.0)
    assert str(e.value) == "DAILY_BUDGET_USD must be >= 0"


def test_ops_default_keep_is_bounded_at_10000():
    ops = obs.Ops(1.0, clock=lambda: _utc())
    for _ in range(10_001):
        ops.record(_rec())
    assert ops.summary()["requests"] == 10_000


def test_ops_day_is_utc_not_local(monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")  # UTC+14: local date is a day ahead
    time.tzset()
    try:
        ops = obs.Ops(1.0, clock=lambda: _utc(12))
        assert ops._day() == "2026-10-10"
        east = dt.timezone(dt.timedelta(hours=14))
        ops2 = obs.Ops(1.0, clock=lambda: dt.datetime(2026, 10, 10, 0, 30, tzinfo=east))
        assert ops2._day() == "2026-10-09"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_ops_record_rounds_spend_to_6dp():
    ops = obs.Ops(10.0, clock=lambda: _utc())
    ops.record(_rec(cost=1.23456789))
    assert ops.spent_today() == 1.234568


def test_ops_summary_percentiles_and_rates():
    ops = obs.Ops(10.0, clock=lambda: _utc())
    for i in range(1, 101):
        ops.record(_rec(total_ms=float(i)))
    s = ops.summary()
    assert s["latency_ms"] == {"p50": 50.0, "p95": 95.0}

    ops = obs.Ops(10.0, clock=lambda: _utc())
    for o in ("ok", "ok", "failed_closed"):
        ops.record(_rec(outcome=o))
    s = ops.summary()
    assert s["outcomes"] == {"failed_closed": {"count": 1, "rate": 0.3333},
                             "ok": {"count": 2, "rate": 0.6667}}
    assert s["failure_rate"] == 0.3333


# ---- generation -----------------------------------------------------------------
def test_projected_cost_exact(monkeypatch):
    text = "x" * 4_000_000
    chars = len(generation.SYSTEM) + 1 + len(text)
    assert generation.projected_cost("q", [{"text": text}]) == pytest.approx(
        (chars / 4 * 1.0 + 400 * 5.0) / 1e6, rel=1e-12)
    monkeypatch.setitem(generation.PRICE, "input_tokens", 2.0)
    assert generation.projected_cost("q", [{"text": text}]) == pytest.approx(
        (chars / 4 * 2.0 + 400 * 5.0) / 1e6, rel=1e-12)


@pytest.mark.parametrize("bad", ["abc", [1, 2], 5])
def test_answer_non_dict_usage_is_dropped(monkeypatch, bad):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    resp = Mock()
    resp.json.return_value = {"content": [{"text": "hi"}], "usage": bad}
    monkeypatch.setattr(generation, "_call", lambda *a, **k: resp)
    out = generation.answer("q", [{"doc_id": "d", "text": "t"}])
    assert out == "hi" and out.usage == {} and not out.failed


# ---- retrieval ------------------------------------------------------------------
def test_generate_failure_message(monkeypatch):
    monkeypatch.setattr(retrieval, "answer",
                        lambda q, r: generation._generated("x", failed=True))
    with pytest.raises(RuntimeError) as e:
        retrieval._generate("q", [])
    assert str(e.value) == "generation provider failed"


def test_ms_exact(monkeypatch):
    monkeypatch.setattr(retrieval, "perf_counter", lambda: 1.23456)
    assert retrieval._ms(1.0) == 234.6
