"""Durable permission reconciliation with section restrictions and retry safety.

The JSONL source is a patch feed: missing documents are unchanged. Explicit
{ "doc_id": "...", "deleted": true } removes a document from both stores.
"""
from __future__ import annotations
import json
import logging
import time
from pathlib import Path

from . import config
from .ingest import strictest, validate_acl
from .retrieval import clear_cache
from .store import ChunkACL, ChunkPolicy, SourceCheckpoint, permission_transaction
from .vectorstore import update_chunk_acl, delete_doc

log = logging.getLogger("permrag")


def read_source(path: str | Path = None):
    desired = {}
    for line in Path(path or config.PERMISSIONS_SOURCE).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        doc_id = row["doc_id"]
        if not isinstance(doc_id, str) or not doc_id.strip() or doc_id in desired:
            raise ValueError("invalid or duplicate document id")
        if "deleted" in row and not isinstance(row["deleted"], bool):
            raise ValueError("deleted must be a boolean")
        desired[doc_id] = None if row.get("deleted") else validate_acl(row.get("acl"))
    return desired


def sync_once(source_path: str | Path = None) -> list[str]:
    # Mark pending even if source parsing fails: permissions cannot be trusted
    # after an attempted reconciliation fails. A subsequent pass repairs it.
    with permission_transaction() as (s, state):
        state.revision += 1
        intent = state.revision
        state.pending = True
    changed = set()
    with permission_transaction() as (s, state):
        if state.revision != intent:
            raise RuntimeError("reconciliation superseded by a newer mutation")
        if state.rebuild_required:
            raise RuntimeError("full reingestion required")
        rows = s.query(ChunkACL).all()
        checkpoint = None
        if source_path is not None or config.PERMISSIONS_BACKEND == "jsonl":
            desired = read_source(source_path)
        elif config.PERMISSIONS_BACKEND == "gdrive":
            from .connectors.gdrive import build_drive_client, load_drive_changes
            drive = build_drive_client(config.DRIVE_CREDENTIALS_FILE, config.DRIVE_SUBJECT or None)
            checkpoint = s.get(SourceCheckpoint, "gdrive")
            desired, token = load_drive_changes(
                drive, checkpoint.token if checkpoint else None,
                sorted({row.doc_id for row in rows}))
            if not isinstance(token, str) or not token:
                raise ValueError("Drive checkpoint missing")
            if checkpoint is None:
                checkpoint = SourceCheckpoint(provider="gdrive", token=token)
                s.add(checkpoint)
            else:
                checkpoint.token = token
        else:
            raise ValueError("unsupported permissions backend")
        policies = {p.chunk_id: p for p in s.query(ChunkPolicy).all()}
        if any(row.chunk_id not in policies for row in rows):
            raise RuntimeError("legacy index: full reingestion required")
        for row in rows:
            policy = policies[row.chunk_id]
            if row.doc_id in desired:
                acl = desired[row.doc_id]
                if acl is None:
                    delete_doc(row.doc_id)
                    s.delete(row)
                    s.delete(policy)
                    changed.add(row.doc_id)
                    continue
                effective = (acl if policy.section_acl is None else
                             strictest(acl, policy.section_acl))
                if policy.doc_acl != acl or row.acl != effective:
                    changed.add(row.doc_id)
                policy.doc_acl = acl
                row.acl = effective
            # Replay every effective ACL, including after a partial Qdrant write
            # followed by SQL rollback or after the source changes again.
            update_chunk_acl(row.chunk_id, row.acl)
        state.revision += 1
        state.pending = False
    clear_cache()
    return sorted(changed)


def watch(interval_s: float = 10.0):
    if interval_s <= 0:
        raise ValueError("poll interval must be positive")
    while True:
        try:
            sync_once()
        except Exception:
            log.exception("sync failed; queries remain blocked until recovery")
        time.sleep(interval_s)
