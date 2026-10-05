"""Behavioral regressions identified by mutation testing."""
import datetime as dt

import pytest

from app import audit, store, sync
from app.identity import Principal


def test_recent_audit_has_complete_schema_newest_order_and_exact_limits(client):
    principal = Principal("auditor@example.test", ("security", "hr"))
    for number in range(52):
        audit.write_audit(principal, f"query-{number}", [number], [f"doc-{number}"],
                          number, number % 2 == 1)
    rows = audit.recent()
    assert len(rows) == 50
    assert [row["query"] for row in rows] == [f"query-{n}" for n in range(51, 1, -1)]
    assert set(rows[0]) == {"ts", "user", "groups", "query", "returned_docs",
                            "denied_count", "fail_closed"}
    timestamp = dt.datetime.fromisoformat(rows[0]["ts"])
    assert timestamp.date() == dt.datetime.now(dt.timezone.utc).date()
    assert rows[0] | {"ts": "timestamp"} == {
        "ts": "timestamp", "user": "auditor@example.test", "groups": ["security", "hr"],
        "query": "query-51", "returned_docs": ["doc-51"], "denied_count": 51,
        "fail_closed": True,
    }
    assert audit.recent(1) == rows[:1]
    assert audit.recent(0) == []


def test_denial_heatmap_counts_only_denials_per_user_and_sorts_totals(client):
    for user, count in [("alpha", 0), ("alpha", 1), ("alpha", 2),
                        ("beta", 4), ("gamma", 0)]:
        audit.write_audit(Principal(user), "query", [], [], count, False)
    assert audit.denied_heatmap() == [
        {"user": "beta", "queries_with_denials": 1, "total_denied_chunks": 4},
        {"user": "alpha", "queries_with_denials": 2, "total_denied_chunks": 3},
    ]


def test_engine_uses_requested_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'independent.db'}"
    engine = store.make_engine(url)
    try:
        assert str(engine.url) == url
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT 41 + 1").scalar_one() == 42
    finally:
        engine.dispose()


@pytest.mark.parametrize("interval", [None, 0.25])
def test_watch_retries_failures_and_preserves_poll_interval(monkeypatch, interval):
    events = []
    class StopWatch(BaseException):
        pass
    def reconcile():
        events.append("sync")
        if events.count("sync") == 1:
            raise OSError("source temporarily unavailable")
    def sleep(seconds):
        events.append(seconds)
        if events.count("sync") == 2:
            raise StopWatch()
    monkeypatch.setattr(sync, "sync_once", reconcile)
    monkeypatch.setattr(sync.time, "sleep", sleep)
    with pytest.raises(StopWatch):
        sync.watch() if interval is None else sync.watch(interval)
    expected = 10.0 if interval is None else interval
    assert events == ["sync", expected, "sync", expected]


def test_generation_provider_receives_complete_context_and_request_contract(monkeypatch):
    import httpx
    from unittest.mock import Mock
    from app import config, generation
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fixture-key")
    monkeypatch.setattr(config, "ANTHROPIC_BASE_URL", "https://provider.example.test")
    monkeypatch.setattr(config, "GENERATION_MODEL", "fixture-model")
    post = Mock(return_value=httpx.Response(
        200, request=httpx.Request("POST", "https://provider.example.test/v1/messages"),
        json={"content": [{"text": "provider response"}]}))
    monkeypatch.setattr(generation.httpx, "post", post)
    assert generation.answer("question", [
        {"doc_id": "one", "text": "first permitted chunk"},
        {"doc_id": "two", "text": "second permitted chunk"},
    ]) == "provider response"
    assert post.call_count == 1
    assert post.call_args.args == ("https://provider.example.test/v1/messages",)
    sent = post.call_args.kwargs
    # HTTP header names are case-insensitive; their values are protocol inputs.
    assert httpx.Headers(sent["headers"]) == httpx.Headers({
        "x-api-key": "fixture-key", "anthropic-version": "2023-06-01"})
    assert sent["timeout"] == 30
    assert sent["json"] == {
        "model": "fixture-model", "max_tokens": 400, "system": generation.SYSTEM,
        "messages": [{"role": "user", "content":
                      "Context:\n[one] first permitted chunk\n\n[two] second permitted chunk\n\nQuestion: question"}],
    }


@pytest.mark.parametrize("body, expected", [
    ({"content": [{"text": "first"}, {"type": "metadata"}, {"text": "second"}]}, "firstsecond"),
    ({}, ""),
])
def test_generation_handles_multiple_text_blocks_and_empty_provider_content(monkeypatch, body, expected):
    import httpx
    from unittest.mock import Mock
    from app import config, generation
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fixture-key")
    monkeypatch.setattr(generation.httpx, "post", Mock(return_value=httpx.Response(
        200, request=httpx.Request("POST", "https://provider.example.test"), json=body)))
    assert generation.answer("question", [{"doc_id": "one", "text": "permitted"}]) == expected
