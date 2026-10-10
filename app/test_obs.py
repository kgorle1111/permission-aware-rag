"""Request logs, /audit ops summary and the daily spend cap (reference app).

The log contract: exactly one JSON line per /query or /ask request on logger "permrag",
fixed field set, ids/counts/timings only. Tests run a real server on an ephemeral port.
"""

import datetime as dt
import json
import logging
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import obs
import pytest
import underwriter_server as srv

srv.rag.audit_path = None  # tests must not write the real audit log

CANARY = "CANARY-Zx9-private-question-text"
FIELDS = {
    "request_id": str,
    "route": str,
    "status": int,
    "outcome": str,
    "retrieve_ms": float,
    "llm_ms": float,
    "total_ms": float,
    "tokens_in": int,
    "tokens_out": int,
    "tokens_cached": int,
    "est_cost_usd": float,
    "returned": int,
    "denied": int,
    "unverified_citations": int,
    "uncited_claims": int,
    "error": (str, type(None)),
}
USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3}


def _llm_ok(question, chunks, timeout=60):
    return {
        "answer": "Active [policy-10023]. Unsupported sentence with no citation. See [watchlist].",
        "usage": USAGE,
        "unverified_citations": ["watchlist"],
        "uncited_claims": ["Unsupported sentence with no citation."],
    }


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def api(monkeypatch):
    clock = Clock(dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC))
    monkeypatch.setattr(srv, "OPS", obs.Ops(5.0, clock=clock))
    monkeypatch.setattr(srv, "JWT_SECRET", None)
    monkeypatch.setattr(srv.llm, "ask", _llm_ok)
    srv._hits.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path, data, {"content-type": "application/json", **(headers or {})}
        )
        req.method = method
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    call.clock = clock
    yield call
    server.shutdown()


def request_lines(caplog):
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "permrag" and r.getMessage().startswith("{")
    ]


def one_line(caplog):
    lines = request_lines(caplog)
    assert len(lines) == 1, lines
    rec = lines[0]
    assert set(rec) == set(FIELDS)
    for key, typ in FIELDS.items():
        assert isinstance(rec[key], typ) and not isinstance(rec[key], bool), (key, rec[key])
    assert len(rec["request_id"]) == 32 and int(rec["request_id"], 16) >= 0
    return rec


def ask(call, user="junior", q="policy 10023 status", path="/ask"):
    return call("POST", path, {"user": user, "q": q})


def test_ok_line_has_exact_fields_and_matches_response(api, caplog):
    caplog.set_level(logging.INFO)
    status, body, headers = ask(api)
    rec = one_line(caplog)
    assert status == 200
    assert (rec["route"], rec["status"], rec["outcome"]) == ("/ask", 200, "ok")
    assert (rec["tokens_in"], rec["tokens_out"], rec["tokens_cached"]) == (10, 5, 3)
    assert rec["est_cost_usd"] == 3.5e-05
    assert (rec["unverified_citations"], rec["uncited_claims"], rec["error"]) == (1, 1, None)
    assert rec["returned"] == len(body["results"]) > 0
    assert rec["denied"] == body["denied_chunks"]
    assert rec["total_ms"] >= rec["retrieve_ms"] >= 0 and rec["llm_ms"] >= 0  # sub-0.05 ms rounds to 0.0
    assert body["request_id"] == headers["X-Request-ID"] == rec["request_id"]
    assert body["uncited_claims"] == ["Unsupported sentence with no citation."]


def test_query_route_is_ok_without_llm_fields(api, caplog):
    caplog.set_level(logging.INFO)
    status, body, _ = ask(api, path="/query")
    rec = one_line(caplog)
    assert (status, rec["route"], rec["outcome"], rec["llm_ms"], rec["tokens_in"]) == (
        200,
        "/query",
        "ok",
        0.0,
        0,
    )


def test_no_results(api, caplog):
    caplog.set_level(logging.INFO)
    status, body, _ = ask(api, q="zzzqqq xylophone")
    rec = one_line(caplog)
    assert (status, rec["outcome"], rec["returned"], rec["llm_ms"]) == (200, "no_results", 0, 0.0)
    assert body["request_id"] == rec["request_id"]


def test_failed_closed_logs_class_name_not_message(api, caplog, monkeypatch):
    caplog.set_level(logging.INFO)

    def boom(*a, **k):
        raise RuntimeError(f"db exploded on {CANARY}")

    monkeypatch.setattr(srv.rag, "retrieve", boom)
    status, body, _ = ask(api)
    rec = one_line(caplog)
    assert (status, rec["status"], rec["outcome"], rec["error"], rec["returned"]) == (
        503,
        503,
        "failed_closed",
        "RuntimeError",
        0,
    )
    assert body["request_id"] == rec["request_id"]
    assert CANARY not in json.dumps(rec)


def test_llm_error_is_llm_fallback(api, caplog, monkeypatch):
    caplog.set_level(logging.INFO)

    def down(*a, **k):
        raise TimeoutError(f"upstream said {CANARY}")

    monkeypatch.setattr(srv.llm, "ask", down)
    status, body, _ = ask(api)
    rec = one_line(caplog)
    assert (status, rec["outcome"], rec["error"], rec["tokens_in"]) == (
        200,
        "llm_fallback",
        "TimeoutError",
        0,
    )
    assert body["note"] == "LLM call failed; showing retrieval only."
    assert rec["returned"] == len(body["results"]) > 0


def test_no_api_key_is_llm_fallback(api, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(srv.llm, "ask", lambda *a, **k: None)
    status, body, _ = ask(api)
    rec = one_line(caplog)
    assert (status, rec["outcome"], rec["error"]) == (200, "llm_fallback", None)
    assert body["note"].startswith("Set ANTHROPIC_API_KEY")


def test_rate_limited(api, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(srv, "rate_limited", lambda ip: True)
    status, body, _ = ask(api)
    rec = one_line(caplog)
    assert (status, rec["status"], rec["outcome"], rec["returned"]) == (429, 429, "rate_limited", 0)
    assert body["request_id"] == rec["request_id"]


@pytest.mark.parametrize(
    "send, code",
    [
        (lambda c: c("POST", "/ask", {"q": "policy"}), 400),  # no user
        (lambda c: c("POST", "/ask", {"user": "nobody", "q": "policy"}), 400),  # unknown user
        (lambda c: c("POST", "/ask", {"user": "junior", "q": ""}), 400),  # empty q
        (lambda c: c("POST", "/ask", {"user": "junior", "q": ["list"]}), 400),  # non-string q
        (lambda c: c("POST", "/ask", {"user": "junior", "q": "x" * 1001}), 400),  # too long
        (lambda c: c("POST", "/ask", b"{not json"), 400),  # malformed JSON
        (lambda c: c("POST", "/ask", [1, 2]), 400),  # body not an object
        (lambda c: c("POST", "/ask", b""), 400),  # empty body
        (lambda c: c("GET", "/query?user=junior"), 400),  # GET without q
    ],
)
def test_bad_request_variants(api, caplog, send, code):
    caplog.set_level(logging.INFO)
    status, body, _ = send(api)
    rec = one_line(caplog)
    assert (status, rec["status"], rec["outcome"]) == (code, code, "bad_request")
    assert (rec["returned"], rec["tokens_in"], rec["llm_ms"], rec["retrieve_ms"]) == (0, 0, 0.0, 0.0)
    assert body["request_id"] == rec["request_id"]


def test_missing_token_in_jwt_mode_is_bad_request_401(api, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(srv, "JWT_SECRET", "s3cret")
    status, body, _ = ask(api)
    rec = one_line(caplog)
    assert (status, rec["status"], rec["outcome"]) == (401, 401, "bad_request")


def test_unknown_path_is_not_a_pipeline_request(api, caplog):
    caplog.set_level(logging.INFO)
    assert api("POST", "/nope")[0] == 404
    assert request_lines(caplog) == []


def test_canary_query_appears_in_no_log_record_on_any_path(api, caplog, capfd, monkeypatch):
    caplog.set_level(logging.DEBUG)  # root logger: every logger's records
    q = f"policy 10023 {CANARY}"
    ask(api, q=q)  # ok
    ask(api, q=q, path="/query")
    ask(api, q=f"zzz {CANARY}")  # no results
    monkeypatch.setattr(srv.llm, "ask", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(q)))
    ask(api, q=q)  # llm failure whose message contains the question
    monkeypatch.setattr(srv.rag, "retrieve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(q)))
    ask(api, q=q)  # retrieval failure whose message contains the question
    api("POST", "/ask", {"user": "nobody", "q": q})  # bad request
    assert len(request_lines(caplog)) == 6
    for r in caplog.records:
        blob = " ".join([r.getMessage(), str(r.args), str(r.exc_text), str(r.__dict__.get("msg"))])
        assert CANARY not in blob, (r.name, r.getMessage())
    out, err = capfd.readouterr()
    assert CANARY not in out + err


def test_ops_summary_exact_values_from_seeded_records():
    clock = Clock(dt.datetime(2026, 10, 10, 9, 0, tzinfo=dt.UTC))
    ops = obs.Ops(5.0, clock=clock)

    def add(outcome, ms, cost):
        rec = obs.new_record("/ask")
        rec.update(outcome=outcome, total_ms=ms, est_cost_usd=cost)
        ops.record(rec)

    for ms, cost in [(100, 0.001), (200, 0.002), (300, 0.003), (400, 0.004)]:
        add("ok", ms, cost)
    clock.now = dt.datetime(2026, 10, 11, 0, 0, tzinfo=dt.UTC)
    add("llm_fallback", 1000, 0.0)
    add("failed_closed", 50, 0.0)
    add("bad_request", 1, 0.0)
    add("rate_limited", 1, 0.0)
    assert ops.summary() == {
        "requests": 8,
        "latency_ms": {"p50": 200, "p95": 1000},  # served only: 50,100,200,300,400,1000
        "cost_per_day_usd": {"2026-10-10": 0.01, "2026-10-11": 0.0},
        "outcomes": {
            "bad_request": {"count": 1, "rate": 0.125},
            "failed_closed": {"count": 1, "rate": 0.125},
            "llm_fallback": {"count": 1, "rate": 0.125},
            "ok": {"count": 4, "rate": 0.5},
            "rate_limited": {"count": 1, "rate": 0.125},
        },
        "failure_rate": 0.25,
    }


def test_ops_summary_empty():
    assert obs.Ops(5.0).summary() == {
        "requests": 0,
        "latency_ms": {"p50": None, "p95": None},
        "cost_per_day_usd": {},
        "outcomes": {},
        "failure_rate": 0.0,
    }


def test_percentile_is_nearest_rank():
    data = list(range(1, 11))
    assert [obs.percentile(data, p) for p in (50, 95, 100, 10)] == [5, 10, 10, 1]
    assert obs.percentile([7], 95) == 7 and obs.percentile([], 50) is None


def test_audit_endpoint_serves_ops_summary(api):
    ask(api)  # ok, cost 3.5e-05
    ask(api, q="zzzqqq xylophone")  # no_results
    ask(api, q="")  # bad_request
    status, body, _ = api("GET", "/audit?user=junior")
    ops = body["ops"]
    assert status == 200
    assert ops["requests"] == 3
    assert {o: v["count"] for o, v in ops["outcomes"].items()} == {"bad_request": 1, "no_results": 1, "ok": 1}
    assert ops["cost_per_day_usd"] == {"2026-10-10": 3.5e-05}
    assert ops["failure_rate"] == 0.0
    assert ops["latency_ms"]["p50"] > 0 and ops["latency_ms"]["p95"] >= ops["latency_ms"]["p50"]


# --- daily spend cap -------------------------------------------------------------------------


def projected(user="junior", q="policy 10023 status"):
    return srv.projected_cost(q, srv.rag.retrieve(q, srv.USERS[user], k=4))


def test_projected_cost_is_worst_case_input_plus_max_output():
    chunks = [{"doc_id": "a", "text": "x" * 400}]
    chars = len(srv.llm.SYSTEM_PROMPT) + len("hello") + 400
    expected = (chars / 4 * 1.00 + srv.llm.MAX_TOKENS * 5.00) / 1e6
    assert srv.projected_cost("hello", chunks) == pytest.approx(expected)


def test_under_budget_calls_llm(api, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(srv, "OPS", obs.Ops(5.0, clock=api.clock))
    status, body, _ = ask(api)
    assert one_line(caplog)["outcome"] == "ok" and "answer" in body


def test_at_budget_exactly_still_calls_llm_and_over_skips(api, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    cost = projected()
    monkeypatch.setattr(srv, "OPS", obs.Ops(cost, clock=api.clock))  # spent 0 + projected == cap
    assert one_line_after(caplog, api)["outcome"] == "ok"
    monkeypatch.setattr(srv, "OPS", obs.Ops(cost - 1e-9, clock=api.clock))  # one hair over
    caplog.clear()
    calls = []
    monkeypatch.setattr(srv.llm, "ask", lambda *a, **k: calls.append(1))
    status, body, _ = ask(api)
    rec = one_line(caplog)
    assert calls == []
    assert (status, rec["outcome"], rec["error"], rec["tokens_in"]) == (200, "llm_fallback", None, 0)
    assert body["note"] == "Daily LLM budget reached; showing retrieval only."
    assert len(body["results"]) == rec["returned"] > 0 and "answer" not in body


def one_line_after(caplog, call):
    ask(call)
    return one_line(caplog)


def test_spend_accumulates_then_blocks_then_resets_at_utc_midnight(api, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    cap = projected() + 3.5e-05  # room for exactly one real call (cost 3.5e-05) plus one projection
    monkeypatch.setattr(srv, "OPS", obs.Ops(cap, clock=api.clock))
    api.clock.now = dt.datetime(2026, 10, 10, 23, 59, 59, tzinfo=dt.UTC)
    assert ask(api)[1]["est_cost_usd"] == 3.5e-05  # spent today = 3.5e-05
    assert srv.OPS.spent_today() == 3.5e-05
    caplog.clear()
    status, body, _ = ask(api)  # 3.5e-05 + projected == cap -> still allowed
    assert "answer" in body
    caplog.clear()
    status, body, _ = ask(api)  # now 7.06e-05 spent, over the cap
    assert body["note"].startswith("Daily LLM budget reached")
    assert one_line(caplog)["outcome"] == "llm_fallback"
    api.clock.now = dt.datetime(2026, 10, 11, 0, 0, 0, tzinfo=dt.UTC)  # UTC midnight
    assert srv.OPS.spent_today() == 0.0
    assert "answer" in ask(api)[1]


def test_non_utc_clock_still_resets_on_utc_day(api):
    ops = obs.Ops(1.0, clock=api.clock)
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    rec = obs.new_record("/ask")
    rec.update(outcome="ok", est_cost_usd=0.5)
    api.clock.now = dt.datetime(2026, 10, 11, 5, 29, tzinfo=ist)  # 23:59 UTC on the 10th
    ops.record(rec)
    api.clock.now = dt.datetime(2026, 10, 11, 5, 30, tzinfo=ist)  # 00:00 UTC on the 11th
    assert ops.spent_today() == 0.0 and ops.summary()["cost_per_day_usd"] == {"2026-10-10": 0.5}


def test_negative_budget_is_rejected():
    with pytest.raises(ValueError):
        obs.Ops(-1)
