"""FAIL CLOSED: on any internal error the answer is nothing — never
unfiltered results. (The exact mirror of Cost Forensics' fail-open, because
the failure costs are opposite.) Plus the identity trust boundary.
"""
from conftest import auth, mint
from failclosed_assert import assert_failed_closed

from app import retrieval
from app import config as appconfig


def test_broken_vector_store_degrades_to_acl_filtered_keyword_fallback(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("qdrant down")
    monkeypatch.setattr(retrieval, "search", boom)
    r = client.post("/query", json={"query": "what are the salary bands?"},
                    headers=auth("alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "keyword_fallback" and body["degraded"] is True
    assert "HR-CANARY" not in r.text   # alice cannot read hr-salaries at any level


def test_fail_closed_is_audited(client, monkeypatch):
    from app.store import AuditLog, SessionLocal
    from app.store import PermissionState
    with SessionLocal() as s:
        s.query(PermissionState).update({"pending": True})
        s.commit()
    try:
        client.post("/query", json={"query": "anything"}, headers=auth("alice"))
    finally:
        with SessionLocal() as s:
            s.query(PermissionState).update({"pending": False})
            s.commit()
    with SessionLocal() as s:
        row = s.query(AuditLog).order_by(AuditLog.id.desc()).first()
    assert row.fail_closed is True


def test_missing_token_401(client):
    assert client.post("/query", json={"query": "hi"}).status_code == 401


def test_garbage_token_401(client):
    r = client.post("/query", json={"query": "hi"},
                    headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


def test_expired_token_401(client):
    r = client.post("/query", json={"query": "hi"},
                    headers={"Authorization": f"Bearer {mint('a@b.com', [], ttl_s=-60)}"})
    assert r.status_code == 401


def test_wrong_audience_401(client):
    tok = mint("a@b.com", ["eng"], aud="some-other-service")
    r = client.post("/query", json={"query": "hi"},
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_forged_signature_401(client):
    """Token signed by a DIFFERENT private key must be rejected — the API
    only trusts the IdP whose public key it holds."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tok = mint("ceo@company.com", ["board"], key=attacker_key)
    r = client.post("/query", json={"query": "executive compensation"},
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_client_cannot_self_assert_groups_via_request(client):
    """Groups come from signed claims only — nothing in the request body can
    widen access."""
    r = client.post("/query",
                    json={"query": "salary bands", "groups": ["hr"], "user": "bob"},
                    headers=auth("guest"))
    # unknown body fields are ignored by the schema; guest still sees nothing
    assert all(x["doc_id"] in ("handbook", "company-strategy")
               for x in r.json()["results"])
    assert "HR-CANARY-2b8c1" not in r.text


def test_audit_endpoint_requires_security_group(client):
    assert client.get("/audit", headers=auth("alice")).status_code == 403
    sec = mint("sec@company.com", ["security"])
    r = client.get("/audit", headers={"Authorization": f"Bearer {sec}"})
    assert r.status_code == 200
    assert "recent" in r.json()


# --- Fail-closed is visible: HTTP 503 with a fixed body -----------------------------------
# Every cause below is global (permission state, index fingerprint, SQL, audit) or depends on
# the caller's own request, never on which documents exist or are hidden from the caller. A
# 503 therefore tells a caller "the service is down", the same thing it tells everyone.

def _pending(value: bool):
    from app.store import PermissionState, SessionLocal
    with SessionLocal() as s:
        s.query(PermissionState).update({"pending": value})
        s.commit()


def test_pending_permission_state_returns_503_with_request_id(client):
    _pending(True)
    try:
        r = client.post("/query", json={"query": "anything"}, headers=auth("alice"))
    finally:
        _pending(False)
    assert_failed_closed(r)


def test_fingerprint_mismatch_returns_503(client, monkeypatch):
    monkeypatch.setattr(retrieval, "fingerprint_problem", lambda s: "index mismatch")
    assert_failed_closed(client.post("/query", json={"query": "anything"}, headers=auth("alice")))


def test_audit_failure_returns_503(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("audit down")
    monkeypatch.setattr(retrieval, "write_audit", boom)
    assert_failed_closed(client.post("/query", json={"query": "anything"}, headers=auth("alice")))


def test_sql_lock_timeout_returns_503(client, monkeypatch):
    from contextlib import contextmanager
    from sqlalchemy.exc import OperationalError

    @contextmanager
    def busy():
        raise OperationalError("UPDATE permission_state", {}, Exception("database is locked"))
        yield
    monkeypatch.setattr(retrieval, "permission_transaction", busy)
    assert_failed_closed(client.post("/query", json={"query": "anything"}, headers=auth("alice")))


def test_dependencies_and_keyword_fallback_both_down_returns_503(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(retrieval, "search", boom)
    monkeypatch.setattr(retrieval, "keyword_search", boom)
    assert_failed_closed(client.post("/query", json={"query": "salary bands"}, headers=auth("alice")))


def test_503_is_logged_and_audited_as_failed_closed(client, caplog):
    import json
    import logging
    from app.store import AuditLog, SessionLocal
    caplog.set_level(logging.INFO)
    _pending(True)
    try:
        r = client.post("/query", json={"query": "anything"}, headers=auth("alice"))
    finally:
        _pending(False)
    recs = [json.loads(m) for m in (x.getMessage() for x in caplog.records) if m.startswith("{")]
    ours = [x for x in recs if x.get("request_id") == r.headers["X-Request-ID"]]
    assert len(ours) == 1 and ours[0]["outcome"] == "failed_closed"
    with SessionLocal() as s:
        assert s.query(AuditLog).order_by(AuditLog.id.desc()).first().fail_closed is True


def test_503_is_identical_for_every_caller_and_query(client):
    """No existence oracle: the failure is a function of global state alone."""
    _pending(True)
    try:
        responses = [client.post("/query", json={"query": q}, headers=auth(who))
                     for who in ("alice", "guest", "ceo")
                     for q in ("salary bands", "BOARD-CANARY-5a6f0", "no such document anywhere")]
    finally:
        _pending(False)
    assert {r.status_code for r in responses} == {503}
    assert {tuple(r.json()) for r in responses} == {("error", "request_id")}
    assert {r.json()["error"] for r in responses} == {"retrieval unavailable"}


def test_denied_and_no_match_stay_200_and_identical(client):
    """The oracle guard from the other side: a hidden document and a nonexistent one still look the
    same, and neither is a 503, because neither is a failure."""
    hidden = client.post("/query", json={"query": "executive compensation exec-comp"},
                         headers=auth("guest"))
    nothing = client.post("/query", json={"query": "zzzz qqqq xylophone"}, headers=auth("guest"))
    assert hidden.status_code == nothing.status_code == 200
    assert hidden.json() == nothing.json() == retrieval.EMPTY_RESPONSE
