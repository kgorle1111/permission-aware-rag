"""Optional, read-only Google Drive permission source.

The caller owns persistence of the returned checkpoint and ACL updates. The
initial call snapshots known documents after capturing a Changes API token;
subsequent calls return complete permission snapshots for changed files.
"""
from __future__ import annotations

from collections.abc import Iterable

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"


def normalize_drive_permissions(perms: list[dict]) -> list[str]:
    """Map a complete Drive permission list to the chunk ACL model.

    A malformed or unknown permission makes the entire snapshot deny-all; it
    must not be silently omitted while retaining a broader recognized entry.
    """
    if not isinstance(perms, list) or not perms:
        return []
    acl = []
    for permission in perms:
        if not isinstance(permission, dict) or permission.get("deleted"):
            return []
        kind = permission.get("type")
        if kind == "anyone":
            acl.append("*")
        elif kind in ("user", "group"):
            email = permission.get("emailAddress")
            if not isinstance(email, str) or not email.strip():
                return []
            acl.append(f"{kind}:{email}")
        elif kind == "domain":
            domain = permission.get("domain")
            if not isinstance(domain, str) or not domain.strip():
                return []
            acl.append(f"group:domain-{domain}")
        else:
            return []
    return sorted(set(acl))


def _permissions_for_file(drive, file_id: str) -> list[str]:
    permissions = []
    page_token = None
    seen_tokens = set()
    while True:
        params = {
            "fileId": file_id,
            "fields": "nextPageToken,permissions(type,emailAddress,domain,deleted)",
            "supportsAllDrives": True,
        }
        if page_token:
            params["pageToken"] = page_token
        response = drive.permissions().list(**params).execute()
        if not isinstance(response, dict):
            return []
        page_permissions = response.get("permissions")
        if isinstance(page_permissions, list):
            permissions.extend(page_permissions)
        else:
            # Continue pagination when possible, but the final snapshot remains
            # deny-all because at least one page was malformed.
            permissions.append(None)
        next_token = response.get("nextPageToken")
        if next_token is None:
            return normalize_drive_permissions(permissions)
        if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
            raise ValueError("malformed or repeated Drive permissions page token")
        seen_tokens.add(next_token)
        page_token = next_token


def _start_page_token(drive) -> str:
    response = drive.changes().getStartPageToken(
        supportsAllDrives=True, fields="startPageToken"
    ).execute()
    token = response.get("startPageToken") if isinstance(response, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("Drive did not return a valid start page token")
    return token


def load_drive_changes(drive, saved_token: str | None,
                       known_doc_ids: Iterable[str] | None) -> tuple[dict[str, list[str] | None], str]:
    """Return complete ACL snapshots/deletions and the checkpoint to persist.

    When there is no checkpoint, the start token is captured before reading the
    baseline. Any changes made during the baseline are therefore included on
    the following call. Provider errors propagate so callers cannot commit a
    checkpoint for a partially fetched result.
    """
    if known_doc_ids is None:
        known_ids = None
    else:
        known_ids = list(known_doc_ids)
        if any(not isinstance(doc_id, str) or not doc_id for doc_id in known_ids):
            raise ValueError("known Drive document IDs must be nonempty strings")
        known_ids = set(known_ids)

    if saved_token is None:
        checkpoint = _start_page_token(drive)
        snapshots = {}
        for doc_id in sorted(known_ids or ()):
            snapshots[doc_id] = _permissions_for_file(drive, doc_id)
        return snapshots, checkpoint

    if not isinstance(saved_token, str) or not saved_token:
        raise ValueError("saved Drive page token must be a nonempty string")

    snapshots: dict[str, list[str] | None] = {}
    page_token = saved_token
    seen_page_tokens = {saved_token}
    while True:
        response = drive.changes().list(
            pageToken=page_token,
            fields="nextPageToken,newStartPageToken,changes(fileId,removed,file(trashed))",
            includeItemsFromAllDrives=True,
            includeRemoved=True,
            supportsAllDrives=True,
        ).execute()
        if not isinstance(response, dict) or not isinstance(response.get("changes"), list):
            raise ValueError("malformed Drive changes response; checkpoint not advanced")
        for change in response["changes"]:
            if not isinstance(change, dict):
                raise ValueError("malformed Drive change; checkpoint not advanced")
            doc_id = change.get("fileId")
            if not isinstance(doc_id, str) or not doc_id:
                raise ValueError("malformed Drive change ID; checkpoint not advanced")
            removed = change.get("removed", False)
            if not isinstance(removed, bool):
                raise ValueError("malformed Drive removal marker; checkpoint not advanced")
            if removed:
                if known_ids is None or doc_id in known_ids:
                    snapshots[doc_id] = None
                continue
            file = change.get("file")
            if not isinstance(file, dict):
                raise ValueError("malformed Drive file change; checkpoint not advanced")
            trashed = file.get("trashed", False)
            if not isinstance(trashed, bool):
                raise ValueError("malformed Drive trashed marker; checkpoint not advanced")
            if known_ids is not None and doc_id not in known_ids:
                continue
            if trashed:
                snapshots[doc_id] = None
            else:
                snapshots[doc_id] = _permissions_for_file(drive, doc_id)

        next_page_token = response.get("nextPageToken")
        if next_page_token is not None:
            if (not isinstance(next_page_token, str) or not next_page_token or
                    next_page_token in seen_page_tokens):
                raise ValueError("malformed or repeated Drive page token; checkpoint not advanced")
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token
            continue
        new_token = response.get("newStartPageToken")
        # A changes list without a final token is incomplete from the caller's
        # perspective. Reusing the saved token is safe and causes replay.
        if new_token is not None and (not isinstance(new_token, str) or not new_token):
            raise ValueError("malformed Drive checkpoint token; checkpoint not advanced")
        return snapshots, new_token or saved_token


def sync_changes(drive, saved_page_token: str | None, apply_acl, remove_doc,
                 known_doc_ids: Iterable[str] | None = None) -> str:
    """Apply one fully fetched snapshot/delta; return its checkpoint on success."""
    snapshots, checkpoint = load_drive_changes(drive, saved_page_token, known_doc_ids)
    for doc_id, acl in snapshots.items():
        if acl is None:
            remove_doc(doc_id)
        else:
            apply_acl(doc_id, acl)
    return checkpoint


def build_drive_client(credentials_file: str, delegated_subject: str | None = None):
    """Build a Drive v3 client using a service account and read-only scope."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Google Drive support requires google-auth and google-api-python-client"
        ) from exc

    credentials = service_account.Credentials.from_service_account_file(
        credentials_file, scopes=[DRIVE_READONLY_SCOPE]
    )
    if delegated_subject is not None:
        if not isinstance(delegated_subject, str) or not delegated_subject.strip():
            raise ValueError("delegated_subject must be a nonempty email address")
        credentials = credentials.with_subject(delegated_subject)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)
