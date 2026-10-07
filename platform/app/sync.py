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
from .vectorstore import update_doc_acls, delete_doc

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
    # The durable barrier precedes parsing and all remote writes.
    with permission_transaction() as (s, state):
        state.revision += 1
        intent = state.revision
        state.pending = True
    checkpoint_token = None
    with permission_transaction() as (s, state):
        if state.revision != intent:
            raise RuntimeError("reconciliation superseded by a newer mutation")
        if state.rebuild_required:
            raise RuntimeError("full reingestion required")
        rows = s.query(ChunkACL).all()
        if source_path is not None or config.PERMISSIONS_BACKEND == "jsonl":
            desired = read_source(source_path)
        elif config.PERMISSIONS_BACKEND == "gdrive":
            from .connectors.gdrive import build_drive_client, load_drive_changes
            drive = build_drive_client(config.DRIVE_CREDENTIALS_FILE, config.DRIVE_SUBJECT or None)
            checkpoint = s.get(SourceCheckpoint, "gdrive")
            desired, checkpoint_token = load_drive_changes(
                drive, checkpoint.token if checkpoint else None,
                sorted({row.doc_id for row in rows}))
            if not isinstance(checkpoint_token, str) or not checkpoint_token:
                raise ValueError("Drive checkpoint missing")
        else:
            raise ValueError("unsupported permissions backend")
        deleting = {row.doc_id for row in rows if row.doc_id in desired and desired[row.doc_id] is None}
        # Commit deletion intent BEFORE deleting any vector. A crash or rollback
        # cannot permit a subsequent grant to clear the barrier over missing rows.
        state.rebuild_required = bool(deleting)
    changed = set()
    with permission_transaction() as (s, state):
        if state.revision != intent:
            raise RuntimeError("reconciliation superseded by a newer mutation")
        rows = s.query(ChunkACL).all()
        policies = {p.chunk_id: p for p in s.query(ChunkPolicy).all()}
        if any(row.chunk_id not in policies or policies[row.chunk_id].paragraph_acl is None for row in rows):
            raise RuntimeError("legacy index: full reingestion required")
        for doc_id in sorted(deleting):
            delete_doc(doc_id)
            changed.add(doc_id)
        document_acls = {}
        for row in rows:
            policy = policies[row.chunk_id]
            if row.doc_id in deleting:
                s.delete(row)
                s.delete(policy)
                continue
            if row.doc_id in desired:
                acl = desired[row.doc_id]
                effective = strictest(strictest(acl, policy.section_acl or []), policy.paragraph_acl)
                if policy.doc_acl != acl or row.acl != effective:
                    changed.add(row.doc_id)
                policy.doc_acl = acl
                row.acl = effective
            if row.doc_id in document_acls and document_acls[row.doc_id] != policy.doc_acl:
                raise RuntimeError("inconsistent document policy; full reingestion required")
            document_acls[row.doc_id] = policy.doc_acl
        # Replay all document grants after partial writes/SQL rollback. Child
        # restrictions remain immutable until full reingestion.
        update_doc_acls(document_acls)
        if checkpoint_token is not None:
            checkpoint = s.get(SourceCheckpoint, "gdrive")
            if checkpoint is None:
                s.add(SourceCheckpoint(provider="gdrive", token=checkpoint_token))
            else:
                checkpoint.token = checkpoint_token
        state.revision += 1
        state.pending = False
        state.rebuild_required = False
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
