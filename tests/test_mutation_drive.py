"""Mutation-driven contracts for the optional Drive permission source."""
from unittest.mock import Mock

import pytest

from app.connectors.gdrive import (
    load_drive_changes,
    normalize_drive_permissions,
    sync_changes,
)


def _request(result):
    request = Mock()
    request.execute.return_value = result
    return request


def test_anyone_permission_maps_to_public_and_non_list_snapshots_deny_all():
    assert normalize_drive_permissions([{"type": "anyone"}]) == ["*"]
    assert normalize_drive_permissions(({"type": "anyone"},)) == []


def test_delta_fetch_uses_drive_complete_permission_and_change_query_options():
    drive = Mock()
    drive.changes.return_value.list.return_value = _request({
        "changes": [{"fileId": "file-1", "file": {}}],
        "newStartPageToken": "next-token",
    })
    drive.permissions.return_value.list.return_value = _request({
        "permissions": [{"type": "user", "emailAddress": "reader@example.com"}],
    })

    snapshots, token = load_drive_changes(drive, "saved-token", ["file-1"])

    assert snapshots == {"file-1": ["user:reader@example.com"]}
    assert token == "next-token"
    drive.changes.return_value.list.assert_called_once_with(
        pageToken="saved-token",
        fields="nextPageToken,newStartPageToken,changes(fileId,removed,file(trashed))",
        includeItemsFromAllDrives=True,
        includeRemoved=True,
        supportsAllDrives=True,
    )
    drive.permissions.return_value.list.assert_called_once_with(
        fileId="file-1",
        fields="nextPageToken,permissions(type,emailAddress,domain,deleted)",
        supportsAllDrives=True,
    )


def test_removed_and_unknown_changes_do_not_short_circuit_later_page_entries():
    drive = Mock()
    drive.changes.return_value.list.return_value = _request({
        "changes": [
            {"fileId": "gone", "removed": True},
            {"fileId": "not-indexed", "file": {}},
            {"fileId": "known", "file": {}},
        ],
        "newStartPageToken": "next-token",
    })
    drive.permissions.return_value.list.return_value = _request({
        "permissions": [{"type": "group", "emailAddress": "team@example.com"}],
    })
    applied = []
    removed = []

    token = sync_changes(
        drive, "saved-token", lambda *args: applied.append(args), removed.append,
        known_doc_ids=["gone", "known"],
    )

    assert token == "next-token"
    assert removed == ["gone"]
    assert applied == [("known", ["group:team@example.com"])]
    drive.permissions.return_value.list.assert_called_once()
    assert drive.permissions.return_value.list.call_args.kwargs["fileId"] == "known"


def test_unknown_removed_changes_are_filtered_by_known_document_ids():
    drive = Mock()
    drive.changes.return_value.list.return_value = _request({
        "changes": [{"fileId": "not-indexed", "removed": True}],
        "newStartPageToken": "next-token",
    })
    removed = []

    token = sync_changes(
        drive, "saved-token", Mock(), removed.append, known_doc_ids=["indexed"],
    )

    assert token == "next-token"
    assert removed == []


@pytest.mark.parametrize("token", [17, [], {}])
def test_truthy_non_string_change_page_tokens_are_rejected(token):
    drive = Mock()
    drive.changes.return_value.list.return_value = _request({
        "changes": [], "nextPageToken": token,
    })

    with pytest.raises(ValueError, match="page token"):
        load_drive_changes(drive, "saved-token", [])


@pytest.mark.parametrize("token", [17, [], {}])
def test_truthy_non_string_permission_page_tokens_are_rejected(token):
    drive = Mock()
    drive.changes.return_value.list.return_value = _request({
        "changes": [{"fileId": "file-1", "file": {}}],
        "newStartPageToken": "next-token",
    })
    drive.permissions.return_value.list.side_effect = [
        _request({"permissions": [], "nextPageToken": token}),
    ]

    with pytest.raises(ValueError, match="page token"):
        load_drive_changes(drive, "saved-token", ["file-1"])


def test_truthy_non_string_file_ids_are_rejected_before_snapshot_fetch():
    drive = Mock()
    drive.changes.return_value.list.return_value = _request({
        "changes": [{"fileId": 17, "file": {}}],
        "newStartPageToken": "next-token",
    })

    with pytest.raises(ValueError, match="change ID"):
        load_drive_changes(drive, "saved-token", None)

    drive.permissions.return_value.list.assert_not_called()
