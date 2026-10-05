"""Adversarial edge contracts for the optional Google Drive adapter."""
import builtins
from types import ModuleType
from unittest.mock import Mock

import pytest

from app.connectors import gdrive


def _request(value):
    request = Mock()
    request.execute.return_value = value
    return request


def _drive_with_changes(response):
    drive = Mock()
    drive.changes.return_value.list.return_value = _request(response)
    return drive


def test_domain_permissions_normalize_to_scoped_group_and_malformed_domain_denies():
    assert gdrive.normalize_drive_permissions([
        {"type": "domain", "domain": "example.test"},
    ]) == ["group:domain-example.test"]
    assert gdrive.normalize_drive_permissions([
        {"type": "anyone"}, {"type": "domain", "domain": " "},
    ]) == []


def test_malformed_permissions_page_is_deny_all_even_if_later_page_is_valid():
    drive = _drive_with_changes({
        "changes": [{"fileId": "doc-1", "file": {}}],
        "newStartPageToken": "next-token",
    })
    drive.permissions.return_value.list.side_effect = [
        _request({"permissions": "malformed", "nextPageToken": "acl-page-2"}),
        _request({"permissions": [{"type": "anyone"}]}),
    ]

    snapshots, checkpoint = gdrive.load_drive_changes(drive, "saved-token", ["doc-1"])

    assert snapshots == {"doc-1": []}
    assert checkpoint == "next-token"
    assert [call.kwargs.get("pageToken") for call in
            drive.permissions.return_value.list.call_args_list] == [None, "acl-page-2"]


def test_non_object_permission_response_denies_file():
    drive = _drive_with_changes({
        "changes": [{"fileId": "doc-1", "file": {}}],
        "newStartPageToken": "next-token",
    })
    drive.permissions.return_value.list.return_value = _request([{"type": "anyone"}])

    snapshots, token = gdrive.load_drive_changes(drive, "saved-token", ["doc-1"])

    assert snapshots == {"doc-1": []}
    assert token == "next-token"


@pytest.mark.parametrize("token_response", [None, [], {}, {"startPageToken": ""},
                                              {"startPageToken": 12}])
def test_initial_checkpoint_must_be_a_nonempty_string(token_response):
    drive = Mock()
    drive.changes.return_value.getStartPageToken.return_value = _request(token_response)
    with pytest.raises(ValueError, match="valid start page token"):
        gdrive.load_drive_changes(drive, None, [])


@pytest.mark.parametrize("known_ids", [["", "ok"], [None], [3]])
def test_known_ids_must_be_nonempty_strings(known_ids):
    with pytest.raises(ValueError, match="known Drive document IDs"):
        gdrive.load_drive_changes(Mock(), "saved-token", known_ids)


@pytest.mark.parametrize("saved_token", ["", 4, False])
def test_delta_requires_nonempty_saved_checkpoint(saved_token):
    with pytest.raises(ValueError, match="saved Drive page token"):
        gdrive.load_drive_changes(Mock(), saved_token, [])


@pytest.mark.parametrize("response", [None, [], {}, {"changes": "malformed"}])
def test_malformed_change_page_never_returns_checkpoint(response):
    drive = _drive_with_changes(response)
    with pytest.raises(ValueError, match="malformed Drive changes response"):
        gdrive.load_drive_changes(drive, "saved-token", [])


def test_removed_unknown_document_is_ignored_when_index_scope_is_known():
    drive = _drive_with_changes({
        "changes": [{"fileId": "not-indexed", "removed": True}],
        "newStartPageToken": "next-token",
    })
    snapshots, token = gdrive.load_drive_changes(drive, "saved-token", ["indexed-doc"])
    assert snapshots == {}
    assert token == "next-token"
    drive.permissions.return_value.list.assert_not_called()


@pytest.mark.parametrize("new_token", ["", 7, False])
def test_malformed_final_checkpoint_is_not_returned(new_token):
    drive = _drive_with_changes({"changes": [], "newStartPageToken": new_token})
    with pytest.raises(ValueError, match="malformed Drive checkpoint token"):
        gdrive.load_drive_changes(drive, "saved-token", [])


def test_sync_changes_never_runs_callbacks_or_returns_token_after_page_fault():
    drive = _drive_with_changes({"changes": [None], "newStartPageToken": "must-not-commit"})
    apply_acl = Mock()
    remove_doc = Mock()

    with pytest.raises(ValueError, match="malformed Drive change"):
        gdrive.sync_changes(drive, "saved-token", apply_acl, remove_doc, known_doc_ids=[])

    apply_acl.assert_not_called()
    remove_doc.assert_not_called()


def test_build_client_reports_missing_optional_dependencies(monkeypatch):
    real_import = builtins.__import__

    def missing_google_import(name, *args, **kwargs):
        if name.startswith("google.oauth2") or name.startswith("googleapiclient"):
            raise ImportError("dependency intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_google_import)
    with pytest.raises(RuntimeError, match="requires google-auth"):
        gdrive.build_drive_client("credentials.json")


def _install_google_stubs(monkeypatch):
    google = ModuleType("google")
    oauth2 = ModuleType("google.oauth2")
    service_account = ModuleType("google.oauth2.service_account")
    googleapiclient = ModuleType("googleapiclient")
    discovery = ModuleType("googleapiclient.discovery")
    credentials = Mock()
    service_account.Credentials = Mock()
    service_account.Credentials.from_service_account_file.return_value = credentials
    discovery.build = Mock(return_value="drive-client")
    oauth2.service_account = service_account
    google.oauth2 = oauth2
    googleapiclient.discovery = discovery
    for name, module in {
        "google": google,
        "google.oauth2": oauth2,
        "google.oauth2.service_account": service_account,
        "googleapiclient": googleapiclient,
        "googleapiclient.discovery": discovery,
    }.items():
        monkeypatch.setitem(__import__("sys").modules, name, module)
    return credentials, service_account, discovery


def test_build_client_without_delegation_keeps_service_account_credentials(monkeypatch):
    credentials, service_account, discovery = _install_google_stubs(monkeypatch)

    result = gdrive.build_drive_client("credentials.json")

    assert result == "drive-client"
    credentials.with_subject.assert_not_called()
    assert discovery.build.call_args.kwargs["credentials"] is credentials


@pytest.mark.parametrize("subject", [" ", 123])
def test_build_client_rejects_invalid_delegated_subject(monkeypatch, subject):
    credentials, service_account, discovery = _install_google_stubs(monkeypatch)

    with pytest.raises(ValueError, match="delegated_subject"):
        gdrive.build_drive_client("credentials.json", subject)

    service_account.Credentials.from_service_account_file.assert_called_once()
    credentials.with_subject.assert_not_called()
    discovery.build.assert_not_called()
