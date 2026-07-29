"""Underwriter corpus ACL tests. Run: python3 test_underwriter.py"""
from underwriter_server import USERS, audit_for, rag


def test():
    def docs(user, q, k=7):
        return {r["doc_id"] for r in rag.retrieve(q, USERS[user], k=k)}

    # guidelines are public to every role
    for u in USERS:
        assert "guidelines" in docs(u, "underwriting guideline senior review inspection"), u

    # junior never sees banking, credit memo, or watchlist — even with exact-content queries
    j = docs("junior", "Delgado NSF debt service coverage misrepresentation watchlist")
    assert j.isdisjoint({"bank-delgado", "credit-memo-delgado", "watchlist"}), j

    # senior sees banking + credit memo but not the compliance watchlist
    s = docs("senior", "Delgado bank balance debt service coverage watchlist")
    assert {"bank-delgado", "credit-memo-delgado"} <= s and "watchlist" not in s, s

    # compliance sees the watchlist but not banking financials
    c = docs("compliance", "Delgado watchlist misrepresentation bank balance")
    assert "watchlist" in c and "bank-delgado" not in c, c

    # auditor sees compliance + banking but no policy/claims files
    a = docs("auditor", "Delgado policy claims watchlist bank balance")
    assert {"watchlist", "bank-delgado"} <= a and a.isdisjoint({"policy-10088", "claims-10023"}), a

    # S2: audit trail is scoped — non-audit roles see only their own queries
    assert {e["user"] for e in audit_for(USERS["junior"])} == {"junior"}
    assert {e["user"] for e in audit_for(USERS["senior"])} == {"senior"}
    # the audit group sees everyone (superset: other test files share this rag and add actors)
    assert {e["user"] for e in audit_for(USERS["auditor"])} >= set(USERS)

    print("underwriter ACL tests passed")


if __name__ == "__main__":
    test()
