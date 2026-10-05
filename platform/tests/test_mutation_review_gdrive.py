"""Exact error-text contracts for the Drive connector (kills message mutants).

Operators grep these messages; the "checkpoint not advanced" suffix tells them
the saved token is safe to retry with.
"""
import builtins
from unittest.mock import Mock

import pytest

from app.connectors import gdrive
from tests.test_drive_edge_contracts import _install_google_stubs


def _drive(*pages):
    drive = Mock()
    reqs = []
    for page in pages:
        r = Mock()
        r.execute.return_value = page
        reqs.append(r)
    drive.changes.return_value.list.side_effect = reqs
    return drive


def _msg(call):
    with pytest.raises(ValueError) as exc:
        call()
    return str(exc.value)


CASES = [
    ([{"changes": None}], "malformed Drive changes response; checkpoint not advanced"),
    ([{"changes": [None]}], "malformed Drive change; checkpoint not advanced"),
    ([{"changes": [{"fileId": ""}]}], "malformed Drive change ID; checkpoint not advanced"),
    ([{"changes": [{"fileId": "a", "removed": "yes"}]}],
     "malformed Drive removal marker; checkpoint not advanced"),
    ([{"changes": [{"fileId": "a"}]}], "malformed Drive file change; checkpoint not advanced"),
    ([{"changes": [{"fileId": "a", "file": {"trashed": "no"}}]}],
     "malformed Drive trashed marker; checkpoint not advanced"),
    ([{"changes": [], "nextPageToken": "tok"}],
     "malformed or repeated Drive page token; checkpoint not advanced"),
    ([{"changes": [], "newStartPageToken": ""}],
     "malformed Drive checkpoint token; checkpoint not advanced"),
]


@pytest.mark.parametrize("pages,message", CASES)
def test_load_changes_error_text(pages, message):
    assert _msg(lambda: gdrive.load_drive_changes(_drive(*pages), "tok", None)) == message


def test_load_changes_rejects_bad_known_ids_and_saved_token_with_exact_text():
    assert _msg(lambda: gdrive.load_drive_changes(Mock(), "tok", [""])) == \
        "known Drive document IDs must be nonempty strings"
    assert _msg(lambda: gdrive.load_drive_changes(Mock(), "", [])) == \
        "saved Drive page token must be a nonempty string"


def test_start_page_token_error_text():
    drive = Mock()
    drive.changes.return_value.getStartPageToken.return_value.execute.return_value = {}
    assert _msg(lambda: gdrive.load_drive_changes(drive, None, [])) == \
        "Drive did not return a valid start page token"


def test_permissions_repeated_page_token_error_text():
    drive = Mock()
    drive.permissions.return_value.list.return_value.execute.return_value = {
        "permissions": [], "nextPageToken": ""}
    assert _msg(lambda: gdrive._permissions_for_file(drive, "f")) == \
        "malformed or repeated Drive permissions page token"


def test_build_client_error_texts(monkeypatch):
    _install_google_stubs(monkeypatch)
    assert _msg(lambda: gdrive.build_drive_client("c.json", " ")) == \
        "delegated_subject must be a nonempty email address"

    real_import = builtins.__import__

    def deny(name, *a, **k):
        if name.startswith(("google.oauth2", "googleapiclient")):
            raise ImportError("x")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", deny)
    with pytest.raises(RuntimeError) as exc:
        gdrive.build_drive_client("c.json")
    assert str(exc.value) == \
        "Google Drive support requires google-auth and google-api-python-client"
