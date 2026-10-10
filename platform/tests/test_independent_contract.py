"""Independently preregistered contract checks; expectations come from docs.

Authored without reading runtime source, existing behavioral tests or mutation
results. Interface names were supplied separately solely for fixture setup.
"""

import json
import pytest

from conftest import reingest
from app.identity import Principal
from app.ingest import ingest_corpus
from app.retrieval import retrieve
from app import store, sync


QUERY = "independent cobalt memorandum"


def principal(*groups, user="auditor"):
    return Principal(user_id=user, groups=tuple(groups))


def ids(who):
    response = retrieve(QUERY, who, k=20)
    return {row["doc_id"] for row in response["results"]}


def doc(doc_id, acl, section_acl="absent", para_acl="absent"):
    paragraph = {"text": f"{QUERY} evidence for {doc_id}."}
    section = {"paragraphs": [paragraph]}
    if section_acl != "absent":
        section["acl"] = section_acl
    if para_acl != "absent":
        paragraph["acl"] = para_acl
    return {"doc_id": doc_id, "acl": acl, "sections": [section]}


@pytest.fixture
def install(ingested, tmp_path):
    def run(documents):
        path = tmp_path / "independent-corpus.json"
        path.write_text(json.dumps(documents))
        assert ingest_corpus(path) == len(documents)
    yield run
    reingest()


def patch(tmp_path, rows):
    path = tmp_path / "independent-permissions.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def fail_after_remote(monkeypatch, operation):
    actual = getattr(sync, operation)
    calls = []

    def lost_ack(*args, **kwargs):
        actual(*args, **kwargs)
        calls.append((args, kwargs))
        raise RuntimeError("independent simulated lost acknowledgement")

    monkeypatch.setattr(sync, operation, lost_ack)
    return actual, calls


def failed_sync(source):
    # The documented contract is the durable barrier, not an exception API.
    try:
        sync.sync_once(source)
    except Exception:
        pass


def test_three_disjoint_principals_must_all_be_satisfied(install):
    install([
        doc("public-control", ["*"]),
        doc("three-gates", ["group:doc"], ["group:section"], ["group:para"]),
        doc("explicit-denial", ["*"], ["*"], []),
    ])
    for groups in [(), ("doc",), ("section",), ("para",),
                   ("doc", "section"), ("doc", "para"), ("section", "para")]:
        assert ids(principal(*groups)) == {"public-control"}
    assert ids(principal("doc", "section", "para")) == {
        "public-control", "three-gates"}


def test_exact_user_and_group_tokens_do_not_match_prefixes_or_case(install):
    install([
        doc("public-control", ["*"]),
        doc("exact-user", ["user:auditor"]),
        doc("exact-group", ["group:desk"]),
    ])
    assert ids(principal("desk")) == {"public-control", "exact-user", "exact-group"}
    for user, groups in [("auditor-extra", ("desk-extra",)),
                         ("Auditor", ("Desk",)), ("user:auditor", ("group:desk",))]:
        assert ids(principal(*groups, user=user)) == {"public-control"}


@pytest.mark.parametrize("bad_acl", [[], "group:desk", [17], None])
def test_explicit_invalid_child_acl_denies_while_missing_child_is_public(install, bad_acl):
    install([
        doc("missing-child-control", ["group:desk"]),
        doc("bad-section", ["group:desk"], bad_acl),
        doc("bad-paragraph", ["group:desk"], ["*"], bad_acl),
    ])
    assert ids(principal("desk")) == {"missing-child-control"}
    assert ids(principal("other")) == set()


def test_document_sync_cannot_remove_independent_child_restrictions(install, tmp_path):
    install([doc("public-control", ["*"]),
             doc("narrow-child", ["group:old"], ["group:child"], ["user:auditor"])])
    sync.sync_once(patch(tmp_path, [{"doc_id": "narrow-child", "acl": ["*"]}]))
    assert ids(principal("old")) == {"public-control"}
    assert ids(principal("child")) == {"public-control", "narrow-child"}
    assert ids(principal("child", user="someone-else")) == {"public-control"}


def test_saved_revocation_survives_omission_and_current_bootstrap(install, tmp_path, monkeypatch):
    install([doc("revoked", ["group:desk"]), doc("second", ["group:other"])])
    assert ids(principal("desk")) == {"revoked"}
    actual, calls = fail_after_remote(monkeypatch, "update_doc_acls")
    failed_sync(patch(tmp_path, [{"doc_id": "revoked", "acl": []}]))
    assert calls, "fault must happen after a real remote write"
    assert ids(principal("desk")) == set()
    monkeypatch.setattr(sync, "update_doc_acls", actual)
    store.init_db()
    sync.sync_once(patch(tmp_path, [{"doc_id": "second", "acl": ["group:desk"]}]))
    assert ids(principal("desk")) == {"second"}
    assert ids(principal("other")) == set()


def test_latest_explicit_patch_replaces_saved_grants(install, tmp_path, monkeypatch):
    install([doc("changed", ["group:old"]), doc("public-control", ["*"])])
    actual, calls = fail_after_remote(monkeypatch, "update_doc_acls")
    failed_sync(patch(tmp_path, [{"doc_id": "changed", "acl": ["group:intermediate"]}]))
    assert calls
    assert ids(principal("intermediate")) == set()
    monkeypatch.setattr(sync, "update_doc_acls", actual)
    sync.sync_once(patch(tmp_path, [{"doc_id": "changed", "acl": ["group:final"]}]))
    assert ids(principal("final")) == {"changed", "public-control"}
    for group in ["old", "intermediate"]:
        assert ids(principal(group)) == {"public-control"}


def test_lost_deletion_ack_requires_reingestion_before_regrant(install, tmp_path, monkeypatch):
    documents = [doc("deleted", ["group:desk"]), doc("public-control", ["*"])]
    install(documents)
    assert ids(principal("desk")) == {"deleted", "public-control"}
    actual, calls = fail_after_remote(monkeypatch, "delete_doc")
    failed_sync(patch(tmp_path, [{"doc_id": "deleted", "deleted": True}]))
    assert calls
    monkeypatch.setattr(sync, "delete_doc", actual)
    failed_sync(patch(tmp_path, [{"doc_id": "deleted", "acl": ["*"]}]))
    assert ids(principal("desk")) == set()
    install(documents)
    assert ids(principal("desk")) == {"deleted", "public-control"}
    assert ids(principal()) == {"public-control"}


def test_invalid_source_blocks_even_previously_cached_public_results(install, tmp_path):
    install([doc("public-control", ["*"])])
    assert ids(principal()) == {"public-control"}
    source = tmp_path / "broken.jsonl"
    source.write_text('{"doc_id": 42, "acl": ["*"]}\n')
    try:
        sync.sync_once(source)
    except Exception:
        pass
    assert ids(principal()) == set()
    sync.sync_once(patch(tmp_path, []))
    assert ids(principal()) == {"public-control"}


@pytest.mark.parametrize("missing_table", ["permission_mutation", "pending_source_checkpoint"])
@pytest.mark.parametrize("pending", [False, True])
def test_prejournal_bootstrap_distinguishes_clean_from_uncertain_state(
        install, tmp_path, missing_table, pending):
    documents = [doc("public-control", ["*"]), doc("uncertain", ["group:desk"])]
    install(documents)
    assert ids(principal()) == {"public-control"}
    if pending:
        # Model a pre-journal write that reached the remote index but never SQL.
        sync.update_doc_acls({"uncertain": ["*"]})
    with store.SessionLocal() as session:
        state = session.get(store.PermissionState, 1)
        state.pending = pending
        state.rebuild_required = False
        before_revision = state.revision
        session.commit()
    with store.engine.begin() as connection:
        connection.exec_driver_sql(f"DROP TABLE {missing_table}")
    store.init_db()
    with store.SessionLocal() as session:
        state = session.get(store.PermissionState, 1)
        assert bool(state.rebuild_required) is pending
        assert bool(state.pending) is pending
        assert state.revision == before_revision + int(pending)
    store.init_db()
    with store.SessionLocal() as session:
        assert session.get(store.PermissionState, 1).revision == before_revision + int(pending)
    if pending:
        failed_sync(patch(tmp_path, []))
        assert ids(principal()) == set()
        install(documents)
    assert ids(principal()) == {"public-control"}
    assert ids(principal("desk")) == {"public-control", "uncertain"}
