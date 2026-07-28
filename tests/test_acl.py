"""ACL semantics: matching, strictest-wins inheritance, deny-by-default."""
from app.ingest import OWNER_ONLY, chunk_document, strictest, validate_acl


def test_strictest_public_vs_group_yields_group():
    assert strictest(["*"], ["group:finance"]) == ["group:finance"]
    assert strictest(["group:finance"], ["*"]) == ["group:finance"]


def test_strictest_both_public_stays_public():
    assert strictest(["*"], ["*"]) == ["*"]


def test_strictest_disjoint_groups_collapses_to_owner_only():
    assert strictest(["group:eng"], ["group:hr"]) == OWNER_ONLY


def test_strictest_intersection():
    assert strictest(["group:eng", "user:a@x.com"], ["user:a@x.com", "group:hr"]) == ["user:a@x.com"]


def test_empty_or_malformed_acl_never_becomes_public():
    assert validate_acl([]) == OWNER_ONLY
    assert validate_acl(None) == OWNER_ONLY
    assert validate_acl(["everyone", "admin"]) == OWNER_ONLY  # malformed entries dropped
    assert validate_acl(["group:eng", "banana"]) == ["group:eng"]


def test_chunk_inheritance_with_section_override():
    doc = {
        "doc_id": "d1",
        "acl": ["*"],
        "sections": [
            {"text": "public paragraph"},
            {"acl": ["group:finance"], "text": "secret projections"},
        ],
    }
    chunks = chunk_document(doc)
    assert chunks[0]["acl"] == ["*"]
    assert chunks[1]["acl"] == ["group:finance"]  # strictest won
