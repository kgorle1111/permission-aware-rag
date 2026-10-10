"""FAIL CLOSED: on any internal error the answer is nothing — never
unfiltered results. (The exact mirror of Cost Forensics' fail-open, because
the failure costs are opposite.) Plus the identity trust boundary.
"""
from conftest import auth, mint

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
