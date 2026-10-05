"""Side channels: existence indistinguishability + permission-scoped caching."""
from conftest import auth


def test_forbidden_topic_indistinguishable_from_nonexistent(client):
    """The response for exists-but-forbidden must equal the response for
    doesn't-exist-at-all (timing aside) — no 'results hidden' tells."""
    r_forbidden = client.post("/query", json={"query": "EXEC-CANARY-9d4e2 equity refresh"},
                              headers=auth("guest"))
    r_nothing = client.post("/query", json={"query": "zorblax quantum unicorn farming"},
                            headers=auth("guest"))
    a, b = r_forbidden.json(), r_nothing.json()
    a.pop("latency_ms", None)
    b.pop("latency_ms", None)
    assert a == b
    assert a["results"] == []


def test_cache_is_permission_scoped(client):
    """A privileged user's cached answer must never be served to an
    unprivileged user asking the same question."""
    q = {"query": "what is the CEO's compensation?"}
    r_ceo = client.post("/query", json=q, headers=auth("ceo"))
    assert "EXEC-CANARY-9d4e2" in r_ceo.text  # ceo sees it (and warms the cache)

    r_guest = client.post("/query", json=q, headers=auth("guest"))
    assert "EXEC-CANARY-9d4e2" not in r_guest.text
    # guest may still receive weakly-relevant PUBLIC chunks — never exec-comp
    assert "exec-comp" not in [x["doc_id"] for x in r_guest.json()["results"]]


def test_same_scope_cache_hit_still_audited(client):
    from app.store import AuditLog, SessionLocal
    q = {"query": "how many vacation days do we get?"}
    client.post("/query", json=q, headers=auth("guest"))
    client.post("/query", json=q, headers=auth("guest"))  # cache hit
    with SessionLocal() as s:
        rows = s.query(AuditLog).filter(AuditLog.user_id == "guest@external.com").count()
    assert rows >= 2  # both retrievals in the trail


def test_denied_count_in_audit(client):
    """The insider-risk signal: guest probing for salaries shows denied > 0."""
    from app.store import AuditLog, SessionLocal
    client.post("/query", json={"query": "salary bands range from 90k to 250k"},
                headers=auth("guest"))
    with SessionLocal() as s:
        row = (s.query(AuditLog).filter(AuditLog.user_id == "guest@external.com")
               .order_by(AuditLog.id.desc()).first())
    assert row.denied_count > 0
    assert row.returned_doc_ids == []
