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
from .store import (ChunkACL, ChunkPolicy, SourceCheckpoint, PermissionMutation,
                    PendingSourceCheckpoint, permission_transaction)
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


def _validate_desired(desired):
    """All sources and durable intents use the same fail-closed ACL boundary."""
    if not isinstance(desired, dict):
        raise ValueError("permission changes must be a document mapping")
    clean = {}
    for doc_id, acl in desired.items():
        if not isinstance(doc_id, str) or not doc_id.strip():
            raise ValueError("invalid document id")
        clean[doc_id] = None if acl is None else validate_acl(acl)
    return clean


def _affected_rows(session, documents):
    """Indexed lookups with bounded parameter lists; no global ORM hydration."""
    documents = sorted(documents)
    for offset in range(0, len(documents), 500):
        yield from (session.query(ChunkACL, ChunkPolicy)
                    .outerjoin(ChunkPolicy, ChunkPolicy.chunk_id == ChunkACL.chunk_id)
                    .filter(ChunkACL.doc_id.in_(documents[offset:offset + 500])).all())


def _validate_policy(policy):
    if policy is None or policy.paragraph_acl is None:
        raise RuntimeError("legacy index: full reingestion required")


def _validate_mirror(session):
    """Preserve global corruption checks with SQL-only existence/aggregation."""
    from sqlalchemy import String, cast, func
    missing = (session.query(ChunkACL.chunk_id)
               .outerjoin(ChunkPolicy, ChunkPolicy.chunk_id == ChunkACL.chunk_id)
               .filter((ChunkPolicy.chunk_id.is_(None)) | (ChunkPolicy.paragraph_acl.is_(None)) |
                       (cast(ChunkPolicy.paragraph_acl, String) == "null"))
               .first())
    if missing is not None:
        raise RuntimeError("legacy index: full reingestion required")
    inconsistent = (session.query(ChunkACL.doc_id)
                    .join(ChunkPolicy, ChunkPolicy.chunk_id == ChunkACL.chunk_id)
                    .group_by(ChunkACL.doc_id)
                    .having(func.count(func.distinct(cast(ChunkPolicy.doc_acl, String))) > 1)
                    .first())
    if inconsistent is not None:
        raise RuntimeError("inconsistent document policy; full reingestion required")


def sync_once(source_path: str | Path = None) -> list[str]:
    # Commit the barrier even if source parsing or journaling later fails.
    with permission_transaction() as (s, state):
        state.revision += 1
        intent = state.revision
        state.pending = True
    with permission_transaction() as (s, state):
        if state.revision != intent:
            raise RuntimeError("reconciliation superseded by a newer mutation")
        if state.rebuild_required:
            raise RuntimeError("full reingestion required")
        _validate_mirror(s)
        checkpoint_token = None
        if source_path is not None or config.PERMISSIONS_BACKEND == "jsonl":
            desired = read_source(source_path)
        elif config.PERMISSIONS_BACKEND == "gdrive":
            from .connectors.gdrive import build_drive_client, load_drive_changes
            drive = build_drive_client(config.DRIVE_CREDENTIALS_FILE, config.DRIVE_SUBJECT or None)
            checkpoint = s.get(SourceCheckpoint, "gdrive")
            known = [doc_id for (doc_id,) in s.query(ChunkACL.doc_id).distinct()]
            desired, checkpoint_token = load_drive_changes(
                drive, checkpoint.token if checkpoint else None, sorted(known))
            if not isinstance(checkpoint_token, str) or not checkpoint_token:
                raise ValueError("Drive checkpoint missing")
        else:
            raise ValueError("unsupported permissions backend")
        desired = _validate_desired(desired)
        journal = {row.doc_id: row for row in s.query(PermissionMutation).all()}
        # Omitted documents retain their failed desired intents; latest patch wins.
        combined = _validate_desired({doc_id: row.acl for doc_id, row in journal.items()})
        combined.update(desired)
        policies_by_doc, found = {}, set()
        dirty = set(journal)
        for row, policy in _affected_rows(s, combined):
            _validate_policy(policy)
            found.add(row.doc_id)
            prior = policies_by_doc.setdefault(row.doc_id, policy.doc_acl)
            if prior != policy.doc_acl:
                raise RuntimeError("inconsistent document policy; full reingestion required")
            acl = combined[row.doc_id]
            if acl is None or policy.doc_acl != acl or row.acl != strictest(strictest(acl, policy.section_acl or []), policy.paragraph_acl):
                dirty.add(row.doc_id)
        if set(journal) - found:
            raise RuntimeError("journal document missing; full reingestion required")
        for doc_id in sorted(dirty):
            if doc_id not in journal:
                s.add(PermissionMutation(doc_id=doc_id, acl=combined[doc_id]))
            else:
                journal[doc_id].acl = combined[doc_id]
        deleting = {doc_id for doc_id in dirty if combined[doc_id] is None}
        # Deletion cannot be recovered by an ACL replay after a SQL rollback.
        state.rebuild_required = bool(deleting)
        if checkpoint_token is not None:
            pending_checkpoint = s.get(PendingSourceCheckpoint, "gdrive")
            if pending_checkpoint is None:
                s.add(PendingSourceCheckpoint(provider="gdrive", token=checkpoint_token))
            else:
                pending_checkpoint.token = checkpoint_token
    changed = set()
    with permission_transaction() as (s, state):
        if state.revision != intent:
            raise RuntimeError("reconciliation superseded by a newer mutation")
        journal = _validate_desired({row.doc_id: row.acl for row in s.query(PermissionMutation).all()})
        deleting = {doc_id for doc_id, acl in journal.items() if acl is None}
        for doc_id in sorted(deleting):
            delete_doc(doc_id)
            changed.add(doc_id)
        acl_updates, policy_updates = [], []
        for row, policy in _affected_rows(s, journal):
            _validate_policy(policy)
            acl = journal[row.doc_id]
            if acl is None:
                s.delete(row)
                s.delete(policy)
                continue
            effective = strictest(strictest(acl, policy.section_acl or []), policy.paragraph_acl)
            if policy.doc_acl != acl or row.acl != effective:
                changed.add(row.doc_id)
            acl_updates.append({"chunk_id": row.chunk_id, "acl": effective})
            policy_updates.append({"chunk_id": policy.chunk_id, "doc_acl": acl})
        update_doc_acls({doc_id: acl for doc_id, acl in journal.items() if acl is not None})
        if acl_updates:
            s.bulk_update_mappings(ChunkACL, acl_updates)
            s.bulk_update_mappings(ChunkPolicy, policy_updates)
        for pending_checkpoint in s.query(PendingSourceCheckpoint).all():
            if pending_checkpoint.provider != "gdrive" or not isinstance(pending_checkpoint.token, str) or not pending_checkpoint.token:
                raise ValueError("Drive checkpoint missing")
            checkpoint = s.get(SourceCheckpoint, pending_checkpoint.provider)
            if checkpoint is None:
                s.add(SourceCheckpoint(provider=pending_checkpoint.provider, token=pending_checkpoint.token))
            else:
                checkpoint.token = pending_checkpoint.token
        s.query(PermissionMutation).delete()
        s.query(PendingSourceCheckpoint).delete()
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
