"""The barrier path's dirty check decides which documents get journaled, hence applied.

A document the check wrongly calls clean is never reconciled (a revocation that never lands); one
it wrongly calls dirty is rewritten needlessly. Sources only carry document-level ACLs, so
section/paragraph-only drift is staged by editing the retained policy inputs in SQL while the
flat effective ACL (ChunkACL.acl) is stale, which is exactly what reconciliation must repair.
"""
import json

from conftest import reingest

from app import config, sync
from app.ingest import OWNER_ONLY
from app.store import ChunkACL, ChunkPolicy, SessionLocal


def _chunks(doc_id):
    with SessionLocal() as s:
        return {r.chunk_id: (r.acl, p.section_acl, p.paragraph_acl)
                for r, p in s.query(ChunkACL, ChunkPolicy)
                .join(ChunkPolicy, ChunkPolicy.chunk_id == ChunkACL.chunk_id)
                .filter(ChunkACL.doc_id == doc_id).all()}


def _policy(*chunk_ids, **fields):
    # A document is dirty if ANY chunk drifted, so a drift test must drift every chunk to prove
    # the check itself (not a clean sibling chunk) is what catches it.
    with SessionLocal() as s:
        for chunk_id in chunk_ids:
            for k, v in fields.items():
                setattr(s.get(ChunkPolicy, chunk_id), k, v)
        s.commit()


def _finance_chunk():
    (cid, (acl, section, _)), = [(c, v) for c, v in _chunks("company-strategy").items()
                                 if v[1] == ["group:finance"]]
    assert acl == ["group:finance"]
    return cid


def _spy_updates(monkeypatch):
    seen = []
    real = sync.update_doc_acls
    monkeypatch.setattr(sync, "update_doc_acls", lambda acls, *a, **k: (seen.append(set(acls)), real(acls, *a, **k))[1])
    return seen


def test_section_only_change_is_applied_through_sync(client):
    cid = _finance_chunk()
    try:
        assert sync.sync_once() == []  # source and mirror agree: nothing to do
        _policy(*_chunks("company-strategy"), section_acl=["group:legal"])  # doc ACL ("*") untouched
        assert sync.sync_once() == ["company-strategy"]
        assert {v[0] == ["group:legal"] for v in _chunks("company-strategy").values()} == {True}
        assert sync.sync_once() == []
    finally:
        reingest()


def test_section_only_revocation_to_nobody_is_applied(client):
    cid = _finance_chunk()
    try:
        _policy(*_chunks("company-strategy"), section_acl=[])
        assert sync.sync_once() == ["company-strategy"]
        assert cid in _chunks("company-strategy")
        assert all(v[0] == OWNER_ONLY for v in _chunks("company-strategy").values())
    finally:
        reingest()


def test_paragraph_only_change_is_applied_through_sync(client):
    chunks = _chunks("handbook")
    cid, other = sorted(chunks)[:2]
    assert chunks[cid][0] == ["*"]
    try:
        _policy(*chunks, paragraph_acl=["group:hr"])
        assert sync.sync_once() == ["handbook"]
        after = _chunks("handbook")
        assert after[cid][0] == ["group:hr"] and after[other][0] == ["group:hr"]
    finally:
        reingest()


def test_null_section_policy_is_treated_as_empty_not_crashing(client):
    """NULL section_acl reads as [] (deny, fail closed). A consistent row must stay untouched while
    another document's real change forces the barrier path."""
    cid = _finance_chunk()
    try:
        _policy(cid, section_acl=None)
        with SessionLocal() as s:
            s.get(ChunkACL, cid).acl = OWNER_ONLY  # what the null policy yields: consistent
            s.commit()
        rows = [json.loads(l) for l in open(config.PERMISSIONS_SOURCE) if l.strip()]
        for r in rows:
            if r["doc_id"] == "hr-salaries":
                r["acl"] = ["user:cfo@company.com"]
        open(config.PERMISSIONS_SOURCE, "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
        assert sync.sync_once() == ["hr-salaries"]
        assert _chunks("company-strategy")[cid][0] == OWNER_ONLY
    finally:
        reingest()


def test_only_drifted_documents_are_journaled_and_rewritten(client, monkeypatch):
    """Clean documents (incl. ones with restricted sections) must not be treated as dirty."""
    seen = _spy_updates(monkeypatch)
    rows = [json.loads(l) for l in open(config.PERMISSIONS_SOURCE) if l.strip()]
    for r in rows:
        if r["doc_id"] == "hr-salaries":
            r["acl"] = ["user:cfo@company.com"]
    open(config.PERMISSIONS_SOURCE, "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
    try:
        assert sync.sync_once() == ["hr-salaries"]
        assert seen == [{"hr-salaries"}]
    finally:
        reingest()
