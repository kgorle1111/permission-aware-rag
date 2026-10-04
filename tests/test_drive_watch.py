from unittest.mock import MagicMock

import pytest

from scripts.drive_watch import register


def test_register_writes_private_channel_config_without_overwrite(tmp_path):
    drive = MagicMock()
    drive.changes().getStartPageToken().execute.return_value = {"startPageToken": "start"}
    drive.changes().watch().execute.return_value = {"resourceId": "resource", "expiration": "1234"}
    path = tmp_path / "channel.env"
    register(drive, "https://example.test/webhooks/drive", path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert "DRIVE_WEBHOOK_RESOURCE_ID=resource" in path.read_text()
    with pytest.raises(FileExistsError):
        register(drive, "https://example.test/webhooks/drive", path)
    assert drive.changes().watch.call_count == 2  # setup plus one actual registration


def test_register_rejects_insecure_endpoint_before_creating_file(tmp_path):
    path = tmp_path / "channel.env"
    with pytest.raises(ValueError):
        register(MagicMock(), "http://example.test/webhooks/drive", path)
    assert not path.exists()
