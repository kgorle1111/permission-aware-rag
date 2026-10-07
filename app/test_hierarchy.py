"""Permissions are AND across document, section and paragraph boundaries."""

import pytest
from permission_rag import PermissionRAG

BOB = {"id": "bob", "groups": ["hr"]}
SECTION_ONLY = {"id": "bob", "groups": []}
HR_ONLY = {"id": "alice", "groups": ["hr"]}


def hierarchy(rag):
    rag.add_document(
        "nested",
        "",
        {"group:hr"},
        sections=[
            {
                "acl": ["user:bob"],
                "paragraphs": [
                    {"text": "salary policy ordinary details"},
                    {"text": "salary policy secret compensation", "acl": ["group:executive"]},
                ],
            },
        ],
    )


def test_distinct_memberships_satisfy_all_levels():
    rag = PermissionRAG()
    hierarchy(rag)
    assert [r["text"] for r in rag.retrieve("salary policy", BOB)] == ["salary policy ordinary details"]
    assert rag.retrieve("salary policy", SECTION_ONLY) == []
    assert rag.retrieve("salary policy", HR_ONLY) == []
    executive_bob = {"id": "bob", "groups": ["hr", "executive"]}
    assert len(rag.retrieve("salary policy", executive_bob)) == 2


def test_public_document_does_not_open_restricted_paragraph_or_overlap():
    rag = PermissionRAG()
    rag.add_document(
        "public",
        "public overview",
        ["*"],
        chunk_words=80,
        sections=[
            {
                "paragraphs": [
                    {"text": "public procedure"},
                    {"text": "secret restricted procedure", "acl": ["group:hr"]},
                ]
            },
        ],
    )
    guest = {"id": "guest", "groups": []}
    hits = rag.retrieve("procedure secret", guest, k=20)
    assert hits and all("secret" not in h["text"] for h in hits)
    assert len(rag.chunks) == 3


@pytest.mark.parametrize("bad", [[], "group:hr", ["group:hr\n"], None])
def test_explicit_invalid_level_rejected_atomically(bad):
    rag = PermissionRAG()
    with pytest.raises((TypeError, ValueError), match="acl|iterable"):
        rag.add_document(
            "invalid",
            "public lead",
            ["*"],
            sections=[
                {"text": "valid section"},
                {"paragraphs": [{"text": "restricted", "acl": bad}]},
            ],
        )
    assert rag.chunks == []


def test_hierarchy_isolation_gate_catches_or_and_flatten_bugs():
    from mutants import AnyLevelGrants, FlattenIntersection
    from run_evals import hierarchy_isolation_gate

    assert hierarchy_isolation_gate(PermissionRAG, verbose=False) == (0, 8)
    assert hierarchy_isolation_gate(AnyLevelGrants, verbose=False)[0] > 0
    assert hierarchy_isolation_gate(FlattenIntersection, verbose=False)[0] > 0
