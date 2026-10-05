from types import ModuleType
from unittest.mock import Mock

import pytest

from app.connectors.gdrive import (
    DRIVE_READONLY_SCOPE,
    build_drive_client,
    load_drive_changes,
    sync_changes,
)


def request(value=None, error=None):
    result = Mock()
    if error is not None:
        result.execute.side_effect = error
    else:
        result.execute.return_value = value
    return result


def test_initial_load_captures_token_before_complete_baseline():
    drive = Mock()
    drive.changes.return_value.getStartPageToken.return_value = request(
        {"startPageToken": "start-7"}
    )
    drive.permissions.return_value.list.side_effect = [
        request({"permissions": [{"type": "user", "emailAddress": "a@example.com"}],
                 "nextPageToken": "perm-2"}),
        request({"permissions": [{"type": "group", "emailAddress": "team@example.com"}]}),
    ]

    snapshots, token = load_drive_changes(drive, None, ["doc-1"])

    assert snapshots == {"doc-1": ["group:team@example.com", "user:a@example.com"]}
    assert token == "start-7"
    drive.changes.return_value.getStartPageToken.assert_called_once_with(
        supportsAllDrives=True, fields="startPageToken"
    )
    calls = drive.permissions.return_value.list.call_args_list
    assert [call.kwargs.get("pageToken") for call in calls] == [None, "perm-2"]
    assert all(call.kwargs["fileId"] == "doc-1" for call in calls)


def test_delta_pages_changes_and_fetches_complete_permissions():
    drive = Mock()
    drive.changes.return_value.list.side_effect = [
        request({"changes": [
            {"fileId": "doc-1", "file": {"trashed": False}},
            {"fileId": "doc-2", "removed": True},
        ], "nextPageToken": "changes-2"}),
        request({"changes": [
            {"fileId": "doc-3", "file": {"trashed": True}},
            {"fileId": "doc-1", "file": {}},
        ], "newStartPageToken": "start-9"}),
    ]
    drive.permissions.return_value.list.side_effect = [
        request({"permissions": [{"type": "anyone"}], "nextPageToken": "acl-2"}),
        request({"permissions": [{"type": "user", "emailAddress": "a@example.com"}]}),
        request({"permissions": [{"type": "group", "emailAddress": "team@example.com"}]}),
    ]

    snapshots, token = load_drive_changes(
        drive, "start-8", ["doc-1", "doc-2", "doc-3"]
    )

    assert snapshots == {
        "doc-1": ["group:team@example.com"],
        "doc-2": None,
        "doc-3": None,
    }
    assert token == "start-9"
    change_calls = drive.changes.return_value.list.call_args_list
    assert [call.kwargs["pageToken"] for call in change_calls] == ["start-8", "changes-2"]
    assert [call.kwargs.get("pageToken") for call in
            drive.permissions.return_value.list.call_args_list] == [None, "acl-2", None]


def test_sync_changes_callbacks_run_only_after_fetch_and_checkpoint_is_returned():
    drive = Mock()
    drive.changes.return_value.list.return_value = request({
        "changes": [{"fileId": "doc-1", "removed": True}],
        "newStartPageToken": "start-10",
    })
    removed = []

    token = sync_changes(drive, "start-9", Mock(), removed.append)

    assert removed == ["doc-1"]
    assert token == "start-10"


@pytest.mark.parametrize("change", [
    None,
    {},
    {"fileId": "doc-1"},
    {"fileId": "doc-1", "removed": "yes"},
    {"fileId": "doc-1", "file": {"trashed": "yes"}},
])
def test_malformed_changes_reject_the_delta_without_returning_checkpoint(change):
    drive = Mock()
    drive.changes.return_value.list.return_value = request({
        "changes": [change], "newStartPageToken": "must-not-advance",
    })

    with pytest.raises(ValueError, match="malformed"):
        load_drive_changes(drive, "saved-token", [])


def test_provider_error_does_not_produce_checkpoint():
    drive = Mock()
    error = RuntimeError("Drive API unavailable")
    drive.changes.return_value.list.return_value = request(error=error)

    with pytest.raises(RuntimeError, match="Drive API unavailable"):
        load_drive_changes(drive, "saved-token", [])


def test_unknown_changed_document_is_ignored_without_permission_fetch():
    drive = Mock()
    drive.changes.return_value.list.return_value = request({
        "changes": [{"fileId": "not-indexed", "file": {}}],
        "newStartPageToken": "next-token",
    })

    snapshots, token = load_drive_changes(drive, "saved-token", ["indexed-doc"])

    assert snapshots == {}
    assert token == "next-token"
    drive.permissions.return_value.list.assert_not_called()


def test_repeated_page_token_fails_instead_of_looping():
    drive = Mock()
    drive.changes.return_value.list.side_effect = [
        request({"changes": [], "nextPageToken": "page-2"}),
        request({"changes": [], "nextPageToken": "page-2"}),
    ]

    with pytest.raises(ValueError, match="repeated"):
        load_drive_changes(drive, "saved-token", [])

    assert drive.changes.return_value.list.call_count == 2


def test_repeated_permissions_page_token_fails_instead_of_looping():
    drive = Mock()
    drive.changes.return_value.list.return_value = request({
        "changes": [{"fileId": "doc-1", "file": {}}],
        "newStartPageToken": "next-token",
    })
    drive.permissions.return_value.list.side_effect = [
        request({"permissions": [{"type": "user", "emailAddress": "a@example.com"}],
                 "nextPageToken": "acl-2"}),
        request({"permissions": [{"type": "user", "emailAddress": "b@example.com"}],
                 "nextPageToken": "acl-2"}),
    ]

    with pytest.raises(ValueError, match="repeated"):
        load_drive_changes(drive, "saved-token", ["doc-1"])

    assert drive.permissions.return_value.list.call_count == 2


def test_malformed_permission_snapshot_denies_all_but_uses_complete_pages():
    drive = Mock()
    drive.changes.return_value.list.return_value = request({
        "changes": [{"fileId": "doc-1", "file": {}}],
        "newStartPageToken": "next-token",
    })
    drive.permissions.return_value.list.side_effect = [
        request({"permissions": [{"type": "anyone"}, {"type": "unknown"}],
                 "nextPageToken": "acl-2"}),
        request({"permissions": [{"type": "user", "emailAddress": "a@example.com"}]}),
    ]

    snapshots, token = load_drive_changes(drive, "saved-token", ["doc-1"])

    assert snapshots == {"doc-1": []}
    assert token == "next-token"
    assert drive.permissions.return_value.list.call_count == 2


def test_build_client_uses_readonly_scope_and_optional_delegation(monkeypatch):
    google = ModuleType("google")
    oauth2 = ModuleType("google.oauth2")
    service_account = ModuleType("google.oauth2.service_account")
    googleapiclient = ModuleType("googleapiclient")
    discovery = ModuleType("googleapiclient.discovery")
    credentials = Mock()
    delegated_credentials = Mock()
    credentials.with_subject.return_value = delegated_credentials
    service_account.Credentials = Mock()
    service_account.Credentials.from_service_account_file.return_value = credentials
    built = Mock()
    discovery.build = built
    oauth2.service_account = service_account
    google.oauth2 = oauth2
    googleapiclient.discovery = discovery
    monkeypatch.setitem(__import__("sys").modules, "google", google)
    monkeypatch.setitem(__import__("sys").modules, "google.oauth2", oauth2)
    monkeypatch.setitem(__import__("sys").modules, "google.oauth2.service_account", service_account)
    monkeypatch.setitem(__import__("sys").modules, "googleapiclient", googleapiclient)
    monkeypatch.setitem(__import__("sys").modules, "googleapiclient.discovery", discovery)

    result = build_drive_client("credentials.json", "admin@example.com")

    service_account.Credentials.from_service_account_file.assert_called_once_with(
        "credentials.json", scopes=[DRIVE_READONLY_SCOPE]
    )
    credentials.with_subject.assert_called_once_with("admin@example.com")
    built.assert_called_once_with(
        "drive", "v3", credentials=delegated_credentials, cache_discovery=False
    )
    assert result is built.return_value
