"""Revocation & staleness: the measured security property.

Revoke access at the (synthetic) source, run sync, and assert the revoked
user stops seeing the content — under a stated propagation bound.
"""
import json
import time
from pathlib import Path

from conftest import auth, reingest, reset_permission_source

from app import config
from app.sync import sync_once

STALENESS_BOUND_S = 5.0  # one sync pass; polling interval adds to this in prod


def _write_source(acls: dict):
    lines = [json.dumps({"doc_id": d, "acl": a}) for d, a in acls.items()]
    Path(config.PERMISSIONS_SOURCE).write_text("\n".join(lines) + "\n")


def test_doc_revocation_propagates_under_bound(client):
    try:
        q = {"query": "salary bands range from 90k to 250k"}
        assert "hr-salaries" in [x["doc_id"] for x in
                                 client.post("/query", json=q, headers=auth("bob")).json()["results"]]

        # upstream revokes HR's access to the salaries doc
        _write_source({"hr-salaries": ["user:cfo@company.com"]})
        t0 = time.perf_counter()
        changed = sync_once()
        elapsed = time.perf_counter() - t0
        assert "hr-salaries" in changed
        assert elapsed < STALENESS_BOUND_S, f"revocation took {elapsed:.2f}s"

        r = client.post("/query", json=q, headers=auth("bob"))
        assert r.json()["results"] == []
        assert "HR-CANARY-2b8c1" not in r.text
    finally:
        reingest()


def test_revocation_also_kills_cached_answers(client):
    try:
        q = {"query": "what are the salary bands?"}
        first = client.post("/query", json=q, headers=auth("bob"))
        assert "hr-salaries" in [x["doc_id"] for x in first.json()["results"]]  # cached now

        _write_source({"hr-salaries": ["user:cfo@company.com"]})
        sync_once()

        r = client.post("/query", json=q, headers=auth("bob"))
        assert "HR-CANARY-2b8c1" not in r.text
        assert r.json()["results"] == []
    finally:
        reingest()


def test_group_change_loses_all_group_content(client):
    """User removed from a group (new token without it) loses every chunk that
    group could read — identity is the token's signed claims, nothing else."""
    from conftest import mint
    downgraded = mint("bob@company.com", groups=[])  # bob, no longer in hr
    r = client.post("/query", json={"query": "salary bands range from 90k to 250k"},
                    headers={"Authorization": f"Bearer {downgraded}"})
    assert r.json()["results"] == []
    assert "HR-CANARY-2b8c1" not in r.text
