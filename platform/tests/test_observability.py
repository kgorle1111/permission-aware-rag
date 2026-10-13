"""Request logs, /audit ops summary, daily spend cap, and the LLM-outage breaker path (platform).

Contract: exactly one JSON line per /query request on logger "permrag", fixed field set,
ids/counts/timings only. No network: the model is a mocked httpx.post.
"""
import datetime as dt
import json
import logging
from unittest.mock import Mock

import httpx
import pytest

from app import config, generation, observability, resilience, retrieval
from app.identity import Principal

from conftest import auth, mint

CANARY = "CANARY-Zx9-private-question-text"
FIELDS = {
    "request_id": str, "outcome": str, "retrieve_ms": float, "llm_ms": float, "total_ms": float,
    "tokens_in": int, "tokens_out": int, "tokens_cached": int, "est_cost_usd": float,
    "returned": int, "denied": int, "unverified_citations": int, "source": str, "degraded": bool,
}
USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3}


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def ops(monkeypatch):
    clock = Clock(dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(observability, "OPS", observability.Ops(5.0, clock=clock))
    resilience.reset_breakers()
    yield clock
    resilience.reset_breakers()


def lines(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "permrag" and r.getMessage().startswith("{")]


def one_line(caplog):
    got = lines(caplog)
    assert len(got) == 1, got
    rec = got[0]
    assert set(rec) == set(FIELDS)
    for key, typ in FIELDS.items():
        assert isinstance(rec[key], typ) and (typ is bool or not isinstance(rec[key], bool)), (key, rec[key])
    assert len(rec["request_id"]) == 32 and int(rec["request_id"], 16) >= 0
    return rec


def query(client, q="vacation policy", who="guest"):
    return client.post("/query", json={"query": q}, headers=auth(who))


def llm_reply(text="Twenty days [handbook]. Nothing else is stated anywhere.", usage=USAGE):
    return httpx.Response(200, json={"content": [{"text": text}], "usage": usage},
                          request=httpx.Request("POST", "https://example.test"))


@pytest.fixture
def model(monkeypatch):
    """Keyed generation with a scripted transport; returns the call list and the script."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(generation.time, "sleep", lambda s: None)
    calls, script = [], []

    def post(url, **kw):
        calls.append(kw["json"])
        nxt = script.pop(0) if script else llm_reply()
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(generation.httpx, "post", post)
    return calls, script


def test_ok_extractive_line_and_header(client, ops, caplog):
    caplog.set_level(logging.INFO)
    r = query(client)
    rec = one_line(caplog)
    assert r.status_code == 200
    assert r.headers["X-Request-ID"] == rec["request_id"]
    assert "request_id" not in r.json()  # bodies stay byte-identical across fail-closed / no-match
    assert (rec["outcome"], rec["source"], rec["degraded"]) == ("ok", "full_rag", False)
    assert rec["returned"] == len(r.json()["results"]) > 0
    assert (rec["tokens_in"], rec["tokens_out"], rec["tokens_cached"], rec["est_cost_usd"]) == (0, 0, 0, 0.0)
    assert rec["total_ms"] >= rec["retrieve_ms"] > 0


def test_llm_ok_counts_tokens_cost_and_unverified_citations(client, ops, caplog, model):
    caplog.set_level(logging.INFO)
    model[1].append(llm_reply("Twenty days [handbook]. See also [hr-salaries]."))
    assert query(client).status_code == 200
    rec = one_line(caplog)
    assert (rec["outcome"], rec["tokens_in"], rec["tokens_out"], rec["tokens_cached"]) == ("ok", 10, 5, 3)
    assert rec["est_cost_usd"] == 3.5e-05
    assert rec["unverified_citations"] == 1
    assert rec["llm_ms"] >= 0


def test_redaction_placeholders_are_not_unverified_citations(client, ops, caplog, model):
    caplog.set_level(logging.INFO)
    model[1].append(llm_reply("Mail [REDACTED:EMAIL] said twenty days [handbook]."))
    query(client)
    assert one_line(caplog)["unverified_citations"] == 0


def test_cache_hit_logs_zero_spend(client, ops, caplog, model):
    query(client)
    caplog.clear()
    caplog.set_level(logging.INFO)
    query(client)
    rec = one_line(caplog)
    assert (rec["outcome"], rec["tokens_in"], rec["est_cost_usd"], rec["llm_ms"]) == ("ok", 0, 0.0, 0.0)
    assert len(model[0]) == 1


def test_no_results(client, ops, caplog):
    caplog.set_level(logging.INFO)
    r = query(client, "zzzz qqqq xylophone")
    rec = one_line(caplog)
    assert r.json()["results"] == []
    assert (rec["outcome"], rec["returned"], rec["source"]) == ("no_results", 0, "full_rag")


def test_failed_closed_exception_path(client, ops, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(retrieval, "fingerprint_problem", lambda s: "index mismatch")
    r = query(client)
    rec = one_line(caplog)
    assert r.json() == retrieval.EMPTY_RESPONSE  # indistinguishable from "nothing matched" to the caller
    assert (rec["outcome"], rec["source"], rec["returned"], rec["degraded"]) == ("failed_closed", "unavailable", 0, True)


def test_failed_closed_when_keyword_fallback_also_fails(client, ops, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(retrieval, "keyword_search", Mock(side_effect=RuntimeError("sql down")))
    query(client)
    rec = one_line(caplog)
    assert (rec["outcome"], rec["source"], rec["degraded"]) == ("failed_closed", "unavailable", True)


def test_keyword_fallback_is_degraded(client, ops, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("down")))
    r = query(client)
    rec = one_line(caplog)
    assert (rec["outcome"], rec["source"], rec["degraded"], rec["denied"]) == ("degraded", "keyword_fallback", True, 0)
    assert rec["returned"] == len(r.json()["results"]) > 0


def test_denied_count_is_logged(client, ops, caplog):
    from app.embeddings import embed_one
    from app.vectorstore import search_unfiltered_count
    alice = Principal("alice@company.com", ("eng",))
    q = "salary bands compensation"
    expected = search_unfiltered_count(embed_one(q), alice.principals, top_k=config.TOP_K)
    assert expected > 0  # the query does reach rows alice may not read
    caplog.set_level(logging.INFO)
    query(client, q, who="alice")
    assert one_line(caplog)["denied"] == expected


@pytest.mark.parametrize("send, code", [
    (lambda c: c.post("/query", json={"query": "hi"}), 401),
    (lambda c: c.post("/query", json={"query": "hi"}, headers={"Authorization": "Bearer junk"}), 401),
    (lambda c: c.post("/query", json={"query": "   "}, headers=auth("guest")), 422),
    (lambda c: c.post("/query", json={"query": ""}, headers=auth("guest")), 422),
    (lambda c: c.post("/query", json={"query": "x" * 2001}, headers=auth("guest")), 422),
    (lambda c: c.post("/query", json={"query": "hi", "k": 99}, headers=auth("guest")), 422),
    (lambda c: c.post("/query", content=b"{nope", headers={**auth("guest"), "content-type": "application/json"}), 422),
    (lambda c: c.get("/query", headers=auth("guest")), 405),
])
def test_bad_request_variants(client, ops, caplog, send, code):
    caplog.set_level(logging.INFO)
    r = send(client)
    rec = one_line(caplog)
    assert r.status_code == code
    assert (rec["outcome"], rec["source"], rec["degraded"], rec["returned"]) == ("bad_request", "none", False, 0)
    assert rec["request_id"] == r.headers["X-Request-ID"]


def test_non_query_routes_log_nothing(client, ops, caplog):
    caplog.set_level(logging.INFO)
    assert client.get("/healthz").status_code == 200
    assert lines(caplog) == []


def test_canary_query_appears_in_no_log_record_on_any_path(client, ops, caplog, capfd, model, monkeypatch):
    caplog.set_level(logging.DEBUG)
    q = f"vacation policy {CANARY}"
    query(client, q)  # ok via mocked model
    query(client, f"zzzz {CANARY}")  # no results
    model[1].extend([httpx.ConnectError("down")] * 4)  # provider outage
    query(client, f"vacation {CANARY} two")
    monkeypatch.setattr(retrieval, "embed_one", Mock(side_effect=RuntimeError("down")))
    query(client, f"vacation {CANARY} three")  # degraded
    monkeypatch.setattr(retrieval, "keyword_search", Mock(side_effect=RuntimeError("sql down")))
    query(client, f"vacation {CANARY} four")  # failed closed
    monkeypatch.setattr(retrieval, "fingerprint_problem", lambda s: "index mismatch")
    query(client, f"vacation {CANARY} five")  # exception path
    client.post("/query", json={"query": ""}, headers=auth("guest"))  # bad request
    assert len(lines(caplog)) == 7
    for r in caplog.records:
        blob = " ".join([r.getMessage(), str(r.args), str(r.exc_text)])
        assert CANARY not in blob, (r.name, r.getMessage())
    out, err = capfd.readouterr()
    assert CANARY not in out + err


# --- /audit ops summary -------------------------------------------------------------------------


def test_ops_summary_exact_values_from_seeded_records():
    clock = Clock(dt.datetime(2026, 10, 10, 9, 0, tzinfo=dt.timezone.utc))
    ops = observability.Ops(5.0, clock=clock)

    def add(outcome, ms, cost):
        rec = observability.new_record("a" * 32)
        rec.update(outcome=outcome, total_ms=ms, est_cost_usd=cost)
        ops.record(rec)

    for ms, cost in [(100, 0.001), (200, 0.002), (300, 0.003), (400, 0.004)]:
        add("ok", ms, cost)
    clock.now = dt.datetime(2026, 10, 11, 0, 0, tzinfo=dt.timezone.utc)
    add("llm_fallback", 1000, 0.0)
    add("failed_closed", 50, 0.0)
    add("degraded", 70, 0.0)
    add("bad_request", 1, 0.0)
    assert ops.summary() == {
        "requests": 8,
        "latency_ms": {"p50": 200, "p95": 1000},  # served: 50,70,100,200,300,400,1000
        "cost_per_day_usd": {"2026-10-10": 0.01, "2026-10-11": 0.0},
        "outcomes": {
            "bad_request": {"count": 1, "rate": 0.125},
            "degraded": {"count": 1, "rate": 0.125},
            "failed_closed": {"count": 1, "rate": 0.125},
            "llm_fallback": {"count": 1, "rate": 0.125},
            "ok": {"count": 4, "rate": 0.5},
        },
        "failure_rate": 0.375,
    }


def test_ops_summary_empty_and_percentile_nearest_rank():
    assert observability.Ops(5.0).summary() == {
        "requests": 0, "latency_ms": {"p50": None, "p95": None},
        "cost_per_day_usd": {}, "outcomes": {}, "failure_rate": 0.0}
    data = list(range(1, 11))
    assert [observability.percentile(data, p) for p in (50, 95, 100, 10)] == [5, 10, 10, 1]
    assert observability.percentile([7], 95) == 7


def test_audit_serves_ops_from_process_memory(client, ops):
    query(client)
    query(client, "zzzz qqqq xylophone")
    client.post("/query", json={"query": ""}, headers=auth("guest"))
    sec = mint("sec@company.com", ["security"])
    body = client.get("/audit", headers={"Authorization": f"Bearer {sec}"}).json()
    summary = body["ops"]
    assert summary["requests"] == 3
    assert {o: v["count"] for o, v in summary["outcomes"].items()} == {"bad_request": 1, "no_results": 1, "ok": 1}
    assert summary["cost_per_day_usd"] == {"2026-10-10": 0.0}
    assert summary["failure_rate"] == 0.0
    assert set(body) == {"recent", "denied_heatmap", "ops"}


def test_audit_ops_still_403_for_non_security(client, ops):
    assert client.get("/audit", headers=auth("alice")).status_code == 403


# --- daily spend cap ------------------------------------------------------------------------------


def test_projected_cost_is_worst_case_input_plus_max_output():
    chars = len(generation.SYSTEM) + len("hello") + 400
    expected = (chars / 4 * observability.PRICE["input_tokens"]
                + generation.MAX_TOKENS * observability.PRICE["output_tokens"]) / 1e6
    assert generation.projected_cost("hello", [{"doc_id": "a", "text": "x" * 400}]) == pytest.approx(expected)


def test_under_budget_calls_model(client, ops, model, caplog):
    caplog.set_level(logging.INFO)
    r = query(client)
    assert len(model[0]) == 1 and "note" not in r.json()
    assert one_line(caplog)["outcome"] == "ok"


def _set_budget(monkeypatch, clock, usd):
    monkeypatch.setattr(observability, "OPS", observability.Ops(usd, clock=clock))


PROJECTED = 0.002  # pinned so the cap arithmetic is exact; the formula has its own test above


def test_at_budget_calls_model_and_over_budget_skips_with_note(client, ops, model, caplog, monkeypatch):
    monkeypatch.setattr(generation, "projected_cost", lambda q, r: PROJECTED)
    _set_budget(monkeypatch, ops, PROJECTED)  # spent 0 + projected == cap: allowed
    query(client)
    assert len(model[0]) == 1
    retrieval.clear_cache()
    _set_budget(monkeypatch, ops, PROJECTED - 1e-9)  # one hair over: skipped
    caplog.clear()
    caplog.set_level(logging.INFO)
    r = query(client)
    body, rec = r.json(), one_line(caplog)
    assert len(model[0]) == 1  # no second call
    assert body["note"] == "Daily LLM budget reached; showing retrieval only."
    assert (body["source"], body["degraded"]) == ("retrieval_only", True)
    assert body["answer"].startswith(body["results"][0]["text"])
    assert (rec["outcome"], rec["source"], rec["degraded"], rec["tokens_in"]) == ("llm_fallback", "retrieval_only", True, 0)
    assert rec["returned"] == len(body["results"]) > 0


def test_empty_result_stays_canonical_even_over_budget(client, ops, model, monkeypatch):
    _set_budget(monkeypatch, ops, 0.0)
    assert query(client, "zzzz qqqq xylophone").json() == retrieval.EMPTY_RESPONSE
    assert model[0] == []


def test_budget_not_consulted_without_api_key(client, ops, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _set_budget(monkeypatch, ops, 0.0)
    r = query(client)
    assert "note" not in r.json() and r.json()["source"] == "full_rag"


def test_spend_accumulates_blocks_then_resets_at_utc_midnight(client, ops, model, monkeypatch):
    monkeypatch.setattr(generation, "projected_cost", lambda q, r: PROJECTED)
    _set_budget(monkeypatch, ops, PROJECTED + 3.5e-05)  # room for one real call (3.5e-05) plus one projection
    ops.now = dt.datetime(2026, 10, 10, 23, 59, 59, tzinfo=dt.timezone.utc)
    assert "note" not in query(client, "vacation policy a").json()
    assert observability.OPS.spent_today() == 3.5e-05
    assert "note" not in query(client, "vacation policy b").json()  # 3.5e-05 + projected == cap
    blocked = query(client, "vacation policy c").json()  # 7e-05 spent: over
    assert blocked["note"].startswith("Daily LLM budget reached")
    assert len(model[0]) == 2
    ops.now = dt.datetime(2026, 10, 11, 0, 0, 0, tzinfo=dt.timezone.utc)
    assert observability.OPS.spent_today() == 0.0
    assert "note" not in query(client, "vacation policy d").json()
    assert len(model[0]) == 3


def test_negative_budget_is_rejected():
    with pytest.raises(ValueError):
        observability.Ops(-1)


# --- LLM outage is visible to the resilience layer ------------------------------------------------


def test_answer_contract_unchanged_but_failure_and_usage_are_observable(monkeypatch, model):
    docs = [{"doc_id": "d", "text": "permitted"}]
    model[1].append(llm_reply("grounded [d]"))
    ok = generation.answer("q", docs)
    assert ok == "grounded [d]" and isinstance(ok, str)
    assert (ok.failed, ok.usage) == (False, USAGE)
    model[1].append(httpx.ConnectError("down"))
    model[1].extend([httpx.ConnectError("down")] * 3)
    failed = generation.answer("q", docs)
    assert failed == "permitted [d]" and isinstance(failed, str)
    assert (failed.failed, failed.usage) == (True, {})
    assert generation.answer("q", []) == "No results found."


def test_provider_outage_reports_retrieval_only_and_opens_the_breaker(client, ops, model, caplog):
    calls, script = model
    script.extend([httpx.ConnectError("down")] * 40)
    seen = []
    for q in ("vacation one", "vacation two", "vacation three"):
        caplog.clear()
        caplog.set_level(logging.INFO)
        out = query(client, q).json()
        seen.append((out["source"], out["degraded"], one_line(caplog)["outcome"]))
    assert seen == [("retrieval_only", True, "llm_fallback")] * 3
    assert resilience.BREAKERS["llm"].state == "open"
    before = len(calls)
    out = query(client, "vacation four").json()
    assert len(calls) == before  # open breaker: the dead provider is no longer called
    assert (out["source"], out["degraded"]) == ("retrieval_only", True)
    assert out["answer"].startswith(out["results"][0]["text"])


def test_non_retryable_provider_error_also_counts_against_the_breaker(client, ops, model):
    bad = httpx.Response(400, json={"error": {"type": "invalid_request_error"}},
                         request=httpx.Request("POST", "https://example.test"))
    model[1].extend([bad] * 3)
    for q in ("vacation one", "vacation two", "vacation three"):
        assert query(client, q).json()["source"] == "retrieval_only"
    assert resilience.BREAKERS["llm"].state == "open"


def test_healthy_model_keeps_breaker_closed(client, ops, model):
    for q in ("vacation one", "vacation two", "vacation three", "vacation four"):
        assert query(client, q).json()["source"] == "full_rag"
    assert resilience.BREAKERS["llm"].state == "closed"
