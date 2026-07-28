"""Google Drive connector: real-source permission sync.

Uses the two patterns enterprise search products use:
- changes.list with a saved delta token (cheap incremental poll — the polling
  interval is the worst-case staleness bound)
- watch channels / push notifications (best-case near-real-time; point the
  webhook at your /sync endpoint)

Requires: pip install google-api-python-client google-auth
Auth: a service account with domain-wide delegation (the ingestion job must
see every document's REAL permissions, which per-user OAuth cannot).

This module is optional — the synthetic permissions.jsonl source exercises
the identical update path (app/sync.py) without any Google setup.
"""
from __future__ import annotations

OWNER_ONLY = ["user:__owner_only__"]


def normalize_drive_permissions(perms: list[dict]) -> list[str]:
    """Map Drive's permission model onto the chunk ACL model (user:/group:/*)."""
    acl = []
    for p in perms:
        if p.get("type") == "anyone":
            acl.append("*")
        elif p.get("type") == "user" and p.get("emailAddress"):
            acl.append(f"user:{p['emailAddress']}")
        elif p.get("type") == "group" and p.get("emailAddress"):
            acl.append(f"group:{p['emailAddress']}")
        elif p.get("type") == "domain" and p.get("domain"):
            acl.append(f"group:domain-{p['domain']}")
    return acl or list(OWNER_ONLY)


def sync_changes(drive, saved_page_token: str, apply_acl, remove_doc) -> str:
    """One incremental pass against the Drive Changes API.

    drive:      googleapiclient discovery Resource for 'drive' v3
    apply_acl:  callable(doc_id, acl) -> None   (app/sync-style update)
    remove_doc: callable(doc_id) -> None
    Returns the new page token to persist for the next pass.
    """
    resp = drive.changes().list(
        pageToken=saved_page_token,
        fields="newStartPageToken,nextPageToken,"
               "changes(fileId,removed,file(permissions))",
    ).execute()
    for change in resp.get("changes", []):
        if change.get("removed"):
            remove_doc(change["fileId"])
            continue
        perms = (change.get("file") or {}).get("permissions", [])
        apply_acl(change["fileId"], normalize_drive_permissions(perms))
    return resp.get("newStartPageToken") or resp.get("nextPageToken") or saved_page_token
