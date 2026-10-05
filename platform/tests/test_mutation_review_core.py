"""Mutation-review kills for retrieval logging/audit args, ingest ACL merge, audit heatmap."""
import logging
from unittest.mock import Mock

from app import audit, ingest, retrieval, store
from app.identity import Principal


def _messages(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "permrag"]


def test_invalid_k_failure_logs_reason_and_fails_closed(client, caplog):
    with caplog.at_level(logging.ERROR, logger="permrag"):
        assert retrieval.retrieve("q", Principal("u"), k=0) == retrieval.EMPTY_RESPONSE
    assert _messages(caplog) == ["retrieval failed closed"]
    assert "ValueError: k must be between 1 and 20" in caplog.text


def test_pending_permission_state_logs_reconciliation_reason(client, caplog):
    with store.SessionLocal() as s:
        s.query(store.PermissionState).update({"pending": True})
        s.commit()
    try:
        with caplog.at_level(logging.ERROR, logger="permrag"):
            assert retrieval.retrieve("q", Principal("u")) == retrieval.EMPTY_RESPONSE
    finally:
        with store.SessionLocal() as s:
            s.query(store.PermissionState).update({"pending": False})
            s.commit()
    assert "RuntimeError: permission reconciliation required" in caplog.text


def test_fail_closed_audit_failure_is_logged_and_fallback_args_are_exact(client, caplog, monkeypatch):
    spy = Mock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(retrieval, "write_audit", spy)
    with caplog.at_level(logging.ERROR, logger="permrag"):
        assert retrieval.retrieve("q", Principal("u")) == retrieval.EMPTY_RESPONSE
    assert _messages(caplog) == ["retrieval failed closed", "fail-closed audit unavailable"]
    fallback = spy.call_args_list[-1]
    assert fallback.args[2:] == ([], [], 0, True)
    assert fallback.args[4] == 0 and type(fallback.args[4]) is int


def test_success_audit_receives_explicit_denied_and_fail_closed_flag(client, monkeypatch):
    spy = Mock(wraps=retrieval.write_audit)
    monkeypatch.setattr(retrieval, "write_audit", spy)
    retrieval.retrieve("handbook", Principal("u"))
    args = spy.call_args.args
    assert args[5] is False
    assert isinstance(args[4], int)


def test_strictest_wildcard_on_both_sides_is_unrestricted_even_with_extra_entries():
    # Mutants that skip the both-wildcard branch fall through to "return the other ACL".
    assert ingest.strictest(["*"], ["*", "user:x"]) == ["*"]
    assert ingest.strictest(["*", "user:x"], ["*"]) == ["*"]


def test_denied_heatmap_counts_queries_and_sums_denials(client):
    p = Principal("prober")
    for denied in (2, 3):
        audit.write_audit(p, "q", [], [], denied, False)
    audit.write_audit(p, "q", [], [], 0, False)  # excluded: no denials
    audit.write_audit(Principal("other"), "q", [], [], 1, False)
    assert audit.denied_heatmap() == [
        {"user": "prober", "queries_with_denials": 2, "total_denied_chunks": 5},
        {"user": "other", "queries_with_denials": 1, "total_denied_chunks": 1},
    ]
