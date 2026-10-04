from unittest.mock import Mock

import pytest

from app.connectors.gdrive import normalize_drive_permissions, sync_changes


def make_drive(pages):
    drive = Mock()
    requests = []
    for page in pages:
        request = Mock()
        request.execute.return_value = page
        requests.append(request)
    drive.changes.return_value.list.side_effect = requests
    return drive


def test_sync_changes_reads_every_page_and_returns_final_checkpoint():
    drive = make_drive([
        {"changes": [{"fileId": "a", "file": {}}], "nextPageToken": "page-2"},
        {"changes": [{"fileId": "b", "removed": True}],
         "newStartPageToken": "checkpoint-3"},
    ])
    drive.permissions.return_value.list.return_value.execute.return_value = {
        "permissions": [{"type": "user", "emailAddress": "a@example.com"}]
    }
    applied = []
    removed = []

    token = sync_changes(drive, "checkpoint-1", lambda *args: applied.append(args), removed.append)

    assert token == "checkpoint-3"
    assert applied == [("a", ["user:a@example.com"])]
    assert removed == ["b"]
    calls = drive.changes.return_value.list.call_args_list
    assert [call.kwargs["pageToken"] for call in calls] == ["checkpoint-1", "page-2"]


@pytest.mark.parametrize("permissions", [
    None,
    {},
    [],
    [None],
    [{"type": "user"}],
    [{"type": "user", "emailAddress": "a@example.com"}, {"type": "unexpected"}],
    [{"type": "user", "emailAddress": "a@example.com", "deleted": True}],
])
def test_malformed_or_deleted_permissions_deny_all(permissions):
    assert normalize_drive_permissions(permissions) == []


def test_sync_applies_deny_all_for_missing_permissions():
    drive = make_drive([{"changes": [{"fileId": "a", "file": {}}],
                        "newStartPageToken": "checkpoint-2"}])
    applied = []

    token = sync_changes(drive, "checkpoint-1", lambda *args: applied.append(args), Mock())

    assert applied == [("a", [])]
    assert token == "checkpoint-2"


def test_callback_error_propagates_without_returning_checkpoint():
    drive = make_drive([
        {"changes": [{"fileId": "a", "file": {"permissions": [
            {"type": "anyone"}
        ]}}], "nextPageToken": "page-2"},
        {"changes": [], "newStartPageToken": "checkpoint-3"},
    ])
    error = RuntimeError("ACL update failed")

    with pytest.raises(RuntimeError, match="ACL update failed"):
        sync_changes(drive, "checkpoint-1", Mock(side_effect=error), Mock())

    # Fetching may finish before callbacks run, but a callback failure produces
    # no checkpoint return for the caller to persist.
    assert drive.changes.return_value.list.call_count == 2
