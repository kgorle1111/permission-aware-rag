"""Permission sync: keeps index ACL metadata consistent with the source
system. The gap between an upstream revocation and the index reflecting it is
the STALENESS WINDOW — a measured property (tests/test_revocation.py), not a
hand-wave.

Source here is a synthetic permissions.jsonl (one {"doc_id", "acl"} per line)
standing in for Drive/Confluence; app/connectors/gdrive.py is the real-world
connector using the same update path.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import config
from .ingest import validate_acl
from .retrieval import clear_cache
from .store import ChunkACL, SessionLocal
from .vectorstore import update_doc_acl

log = logging.getLogger("permrag")


def read_source(path: str | Path = None) -> dict[str, list[str]]:
    path = Path(path or config.PERMISSIONS_SOURCE)
    acls: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            row = json.loads(line)
            acls[row["doc_id"]] = validate_acl(row["acl"])
    return acls


def sync_once(source_path: str | Path = None) -> list[str]:
    """One reconciliation pass. Returns doc_ids whose ACLs changed.

    Order matters: Postgres (source-of-truth, transactional) first, then the
    Qdrant payload, then the answer cache is invalidated — so a revoked user
    can't keep reading a cached answer after the index was fixed.
    """
    desired = read_source(source_path)
    changed: list[str] = []
    with SessionLocal() as s:
        current: dict[str, list[str]] = {}
        for row in s.query(ChunkACL).all():
            current.setdefault(row.doc_id, row.acl)
        for doc_id, acl in desired.items():
            if doc_id in current and sorted(current[doc_id]) != sorted(acl):
                s.query(ChunkACL).filter_by(doc_id=doc_id).update({"acl": acl})
                changed.append(doc_id)
        s.commit()

    for doc_id in changed:
        update_doc_acl(doc_id, desired[doc_id])
    if changed:
        clear_cache()  # permission-scoped, but revocation must also kill stale answers
        log.info("permission sync applied: %s", changed)
    return changed


def watch(interval_s: float = 10.0):
    """Reconciliation polling loop. interval_s IS your worst-case staleness
    bound — state it, don't hide it. (Webhook push, where the source supports
    it, makes the best case near-real-time; see connectors/gdrive.py.)"""
    log.info("permission sync watching every %.1fs", interval_s)
    while True:
        try:
            sync_once()
        except Exception:
            log.exception("sync pass failed; will retry")
        time.sleep(interval_s)
