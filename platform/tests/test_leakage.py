"""Direct-leakage probes — the strongest possible attack: query for the
EXACT content of a forbidden document, as every unauthorized user, and
assert zero results AND zero canary bytes anywhere in the HTTP response.
"""
from conftest import USERS, auth, forbidden_canaries

PROBES = {
    "eng-oncall": ["production database credentials vault", "ENG-CANARY-7f3a9"],
    "hr-salaries": ["salary bands range from 90k to 250k", "HR-CANARY-2b8c1",
                    "what are the salary bands?"],
    "exec-comp": ["CEO total package $1.2M equity refresh", "EXEC-CANARY-9d4e2",
                  "what is the CEO's compensation?"],
    "board-minutes": ["unannounced acquisition of Northwind Labs", "BOARD-CANARY-5a6f0"],
}


def _may_read(user_docs: set, doc: str) -> bool:
    return doc in user_docs


def test_exact_content_probes_leak_nothing(client):
    for doc, probes in PROBES.items():
        for user, (sub, groups, readable) in USERS.items():
            if _may_read(readable, doc):
                continue  # authorized — covered in test_authorized_users_do_get_answers
            for probe in probes:
                r = client.post("/query", json={"query": probe}, headers=auth(user))
                assert r.status_code == 200
                raw = r.text
                for canary in forbidden_canaries(user):
                    assert canary not in raw, (
                        f"LEAK: {canary} reached {user} probing {doc!r} with {probe!r}")
                assert doc not in [x["doc_id"] for x in r.json()["results"]], (
                    f"LEAK: doc id {doc} returned to {user}")


def test_authorized_users_do_get_answers(client):
    """Security that works by returning nothing to everyone is not security."""
    r = client.post("/query", json={"query": "salary bands range from 90k to 250k"},
                    headers=auth("bob"))
    docs = [x["doc_id"] for x in r.json()["results"]]
    assert "hr-salaries" in docs

    r = client.post("/query", json={"query": "production database credentials vault"},
                    headers=auth("alice"))
    assert "eng-oncall" in [x["doc_id"] for x in r.json()["results"]]

    r = client.post("/query", json={"query": "CEO total package equity refresh"},
                    headers=auth("ceo"))
    assert "exec-comp" in [x["doc_id"] for x in r.json()["results"]]

    # public content reaches everyone, including guest
    r = client.post("/query", json={"query": "how many vacation days do we get?"},
                    headers=auth("guest"))
    assert "handbook" in [x["doc_id"] for x in r.json()["results"]]


def test_section_level_acl_within_public_doc(client):
    """company-strategy is public, but its finance section is finance-only."""
    probe = "revenue target gross margin assumptions FIN-CANARY-4c1d7"
    r = client.post("/query", json={"query": probe}, headers=auth("guest"))
    assert "FIN-CANARY-4c1d7" not in r.text

    r = client.post("/query", json={"query": probe}, headers=auth("carol"))
    assert "FIN-CANARY-4c1d7" in r.text  # finance group reads its own section
