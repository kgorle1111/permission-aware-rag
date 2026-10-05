"""Register an optional Drive push channel; credentials stay in a private env file.

Requires a publicly reachable HTTPS /webhooks/drive deployment and the optional
Google SDK dependencies. This command contacts Google only when explicitly run.
"""
import argparse
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config
from app.connectors.gdrive import build_drive_client


def register(drive, address, output, ttl_hours=24):
    url = urlsplit(address)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("notification address must be a public HTTPS URL without credentials")
    if not 1 <= ttl_hours <= 168:
        raise ValueError("channel lifetime must be between 1 and 168 hours")
    output = Path(output)
    # Reserve the destination before creating a remote channel; never overwrite
    # an existing channel secret or the application's .env file.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        page_token = drive.changes().getStartPageToken().execute()["startPageToken"]
        channel_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        response = drive.changes().watch(pageToken=page_token, body={
            "id": channel_id, "type": "web_hook", "address": address,
            "token": token, "expiration": str(int((time.time() + ttl_hours * 3600) * 1000)),
        }).execute()
        resource_id = response["resourceId"]
        if not isinstance(resource_id, str) or not resource_id:
            raise ValueError("invalid channel resource ID")
        # These are provider-generated identifiers, not shell commands. Validate
        # env values to prevent a malformed response from injecting extra lines.
        if any(c in resource_id for c in "\r\n\x00"):
            raise ValueError("invalid channel resource ID")
        contents = (f"DRIVE_WEBHOOK_CHANNEL_ID={channel_id}\n"
                    f"DRIVE_WEBHOOK_TOKEN={token}\n"
                    f"DRIVE_WEBHOOK_RESOURCE_ID={resource_id}\n")
        with os.fdopen(fd, "w") as stream:
            fd = None
            stream.write(contents)
        return {"id": channel_id, "expiration": response.get("expiration")}
    finally:
        if fd is not None:
            os.close(fd)
            # Keep the reserved file on failure so a potentially-created remote
            # channel cannot be silently registered again by an automatic retry.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--output", type=Path, default=Path(".drive-channel.env"))
    parser.add_argument("--ttl-hours", type=int, default=24)
    args = parser.parse_args()
    drive = build_drive_client(config.DRIVE_CREDENTIALS_FILE, config.DRIVE_SUBJECT or None)
    channel = register(drive, args.address, args.output, args.ttl_hours)
    print(f"Channel settings saved privately to {args.output}; expiration: {channel['expiration']}")
    print("Load these three settings into the API environment and restart it. Keep polling enabled.")


if __name__ == "__main__":
    main()
