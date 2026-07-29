"""Leakage-focused tests. Run: python3 test_permission_rag.py"""

import pathlib
import tempfile

from permission_rag import PermissionRAG

ALICE = {"id": "alice", "groups": ["eng"]}  # engineer
BOB = {"id": "bob", "groups": ["hr"]}  # HR
CAROL = {"id": "carol", "groups": ["eng", "exec"]}  # exec + eng
GUEST = {"id": "guest", "groups": []}


def build():
    rag = PermissionRAG()
    rag.add_document(
        "handbook", "Company handbook: vacation policy is twenty days per year for all employees.", {"*"}
    )
    rag.add_document(
        "arch",
        "Engineering architecture: the payments service uses Postgres and a Redis cache.",
        {"group:eng"},
    )
    rag.add_document(
        "salaries", "HR confidential: salary bands range from 90k to 250k across levels.", {"group:hr"}
    )
    rag.add_document(
        "merger",
        "Executive memo: the acquisition of Acme Corp closes next quarter, keep confidential.",
        {"group:exec", "user:dana"},
    )
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
    assert any(
        r["doc_id"] == "merger" for r in rag.retrieve("acquisition Acme", {"id": "dana", "groups": []})
    )

    # multi-group user sees both
    ids = {r["doc_id"] for r in rag.retrieve("payments acquisition", CAROL, k=5)}
    assert {"arch", "merger"} <= ids

    # audit trail records denials
    entry = rag.audit[-1]
    assert entry["user"] == "carol" and entry["denied_chunks"] >= 1

    # S1: IDF side channel — hidden docs must not shift visible scores.
    # Same visible corpus, one extra hidden doc sharing query terms: scores identical.
    base = build()
    with_hidden = build()
    with_hidden.add_document("hidden", "payments payments postgres redis cache architecture", {"group:hr"})
    a = base.retrieve("payments postgres", ALICE)
    b = with_hidden.retrieve("payments postgres", ALICE)
    assert [(r["id"], r["score"]) for r in a] == [(r["id"], r["score"]) for r in b], (a, b)

    # empty ACL refused at ingest
    try:
        rag.add_document("bad", "text", set())
        raise AssertionError("empty acl accepted")
    except ValueError:
        pass

    # chunking: long docs split at chunk_words with sequential ids, ACL on every chunk
    r3 = PermissionRAG()
    r3.add_document("long", " ".join(f"w{i}" for i in range(200)), {"group:eng"}, chunk_words=80)
    assert [c["id"] for c in r3.chunks] == ["long#0", "long#1", "long#2"]
    assert [len(c["text"].split()) for c in r3.chunks] == [80, 80, 40]
    assert all(c["acl"] == {"group:eng"} for c in r3.chunks)

    # duplicate doc_id refused (nightly re-sync must not silently double-ingest)
    try:
        r3.add_document("long", "again", {"*"})
        raise AssertionError("expected ValueError on duplicate doc_id")
    except ValueError:
        pass

    # audit entries carry timing for value receipts
    r3.retrieve("w5", {"id": "e", "groups": ["eng"]})
    assert r3.audit[-1]["elapsed_ms"] >= 0

    # sentence-boundary chunking: sentences stay whole, boundary sentence overlaps
    r4 = PermissionRAG()
    r4.add_document(
        "s", "One two three four. Five six seven eight. Nine ten eleven twelve.", {"*"}, chunk_words=10
    )
    texts = [c["text"] for c in r4.chunks]
    assert texts[0] == "One two three four. Five six seven eight."
    assert texts[1].startswith("Five six seven eight.")  # overlap carries the boundary sentence

    # remove_document + re-ingest replaces content
    assert r4.remove_document("s") == 2 and r4.chunks == []
    r4.add_document("s", "Replacement text here.", {"*"})
    assert len(r4.chunks) == 1 and r4.remove_document("missing") == 0

    # audit persistence: entries survive a restart via JSONL
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "audit.jsonl"
        r1 = PermissionRAG(audit_path=path)
        r1.add_document("d", "vacation policy details", {"*"})
        r1.retrieve("vacation", ALICE)
        r2 = PermissionRAG(audit_path=path)  # fresh instance = restart
        assert len(r2.audit) == 1 and r2.audit[0]["user"] == "alice"

        # hash chain: intact across restart; detects tampering
        r2.retrieve("vacation", ALICE)
        assert PermissionRAG.verify_audit_chain(path)
        lines = path.read_text().splitlines()
        lines[0] = lines[0].replace("vacation", "salaries")  # tamper first entry
        path.write_text("\n".join(lines) + "\n")
        assert not PermissionRAG.verify_audit_chain(path)

    print(f"all tests passed ({len(rag.audit)} audited retrievals)")


if __name__ == "__main__":
    test()
