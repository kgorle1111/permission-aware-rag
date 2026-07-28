"""Leakage-focused tests. Run: python3 test_permission_rag.py"""
from permission_rag import PermissionRAG

ALICE = {"id": "alice", "groups": ["eng"]}          # engineer
BOB = {"id": "bob", "groups": ["hr"]}               # HR
CAROL = {"id": "carol", "groups": ["eng", "exec"]}  # exec + eng
GUEST = {"id": "guest", "groups": []}


def build():
    rag = PermissionRAG()
    rag.add_document("handbook", "Company handbook: vacation policy is twenty days per year for all employees.", {"*"})
    rag.add_document("arch", "Engineering architecture: the payments service uses Postgres and a Redis cache.", {"group:eng"})
    rag.add_document("salaries", "HR confidential: salary bands range from 90k to 250k across levels.", {"group:hr"})
    rag.add_document("merger", "Executive memo: the acquisition of Acme Corp closes next quarter, keep confidential.", {"group:exec", "user:dana"})
    return rag


def test():
    rag = build()

    # public doc visible to everyone
    for u in (ALICE, BOB, CAROL, GUEST):
        assert any(r["doc_id"] == "handbook" for r in rag.retrieve("vacation policy", u)), u["id"]

    # group scoping
    assert any(r["doc_id"] == "arch" for r in rag.retrieve("payments postgres", ALICE))
    assert not any(r["doc_id"] == "arch" for r in rag.retrieve("payments postgres", BOB))
    assert any(r["doc_id"] == "salaries" for r in rag.retrieve("salary bands", BOB))

    # THE leak test: exact-content query returns nothing the user can't read
    assert rag.retrieve("acquisition of Acme Corp closes next quarter", ALICE) == []
    assert rag.retrieve("salary bands 90k 250k", GUEST) == []

    # user-level ACL entry works
    assert any(r["doc_id"] == "merger" for r in rag.retrieve("acquisition Acme", {"id": "dana", "groups": []}))

    # multi-group user sees both
    ids = {r["doc_id"] for r in rag.retrieve("payments acquisition", CAROL, k=5)}
    assert {"arch", "merger"} <= ids

    # audit trail records denials
    entry = rag.audit[-1]
    assert entry["user"] == "carol" and entry["denied_chunks"] >= 1

    # empty ACL refused at ingest
    try:
        rag.add_document("bad", "text", set())
        assert False, "empty acl accepted"
    except ValueError:
        pass

    print(f"all tests passed ({len(rag.audit)} audited retrievals)")


if __name__ == "__main__":
    test()
