#!/usr/bin/env bash
# Restore a backup made by backup.sh into a NEW Docker volume.
#
#   scripts/restore.sh <backup-dir> <new-volume> [image]
#
# Verifies every sha256 in MANIFEST.sha256 and that data/ holds no unlisted
# files before creating anything; refuses on any mismatch. Refuses to write
# into an existing volume. Image defaults to permission-aware-rag:local; for a
# migration rollback pass the PREVIOUS image (see docs/BACKUP_RESTORE.md).
set -euo pipefail
umask 077

usage() { echo "usage: $0 <backup-dir> <new-volume> [image]" >&2; exit 2; }
[ $# -ge 2 ] && [ $# -le 3 ] || usage
backup=$1
volume=$2
image=${3:-permission-aware-rag:local}

# GNU "hash  path" lines either way; macOS's own /sbin/sha256sum prints BSD tags.
if command -v shasum >/dev/null 2>&1; then hasher=(shasum -a 256); else hasher=(sha256sum); fi

manifest="$backup/MANIFEST.sha256"
[ -s "$manifest" ] || { echo "REFUSING: $manifest is missing or empty" >&2; exit 1; }
[ -s "$backup/data/rag.db" ] || { echo "REFUSING: $backup/data/rag.db is missing" >&2; exit 1; }

if ! (cd "$backup" && "${hasher[@]}" -c MANIFEST.sha256 >/dev/null); then
  (cd "$backup" && "${hasher[@]}" -c MANIFEST.sha256 2>&1 | grep -v ': OK$') >&2 || true
  echo "REFUSING: checksum mismatch in $backup" >&2
  exit 1
fi
listed=$(cut -c67- "$manifest" | LC_ALL=C sort)
present=$(cd "$backup" && find data backup-info.txt -type f | LC_ALL=C sort)
if [ "$listed" != "$present" ]; then
  diff <(echo "$listed") <(echo "$present") >&2 || true
  echo "REFUSING: files in $backup differ from the manifest" >&2
  exit 1
fi
echo "manifest verified: $(wc -l < "$manifest" | tr -d ' ') files" >&2
recorded=$(sed -n 's/^image=//p' "$backup/backup-info.txt")
[ "$recorded" = "$image" ] || echo "note: backup was taken with image $recorded; restoring for $image" >&2

if docker volume inspect "$volume" >/dev/null 2>&1; then
  echo "REFUSING: volume $volume already exists; restore only into a new volume" >&2
  exit 1
fi
started=$(date +%s)
docker volume create "$volume" >/dev/null
created=true
cleanup() { if $created; then docker volume rm "$volume" >/dev/null 2>&1 && echo "removed partial volume $volume" >&2; fi; }
trap cleanup EXIT

# Extract as root so ownership can be set to the image's `app` user, then check
# the restored database as that user, exactly as the service will open it.
tar_opts=()
# macOS bsdtar otherwise ships com.apple.* xattrs that GNU tar in the image warns about.
if tar --version 2>/dev/null | grep -q bsdtar; then tar_opts=(--no-xattrs --no-mac-metadata); fi
COPYFILE_DISABLE=1 tar ${tar_opts[@]+"${tar_opts[@]}"} -C "$backup/data" -cf - . |
  docker run --rm -i --network none --user 0:0 --volume "$volume:/data" --entrypoint sh "$image" -c '
    set -eu
    tar -xf - -C /data
    chown -R app:app /data
    chmod 700 /data
    find /data -type f -name "*.pem" -exec chmod 600 {} +
  '
docker run --rm --network none --volume "$volume:/data:ro" --entrypoint python "$image" -c '
import sqlite3
db = sqlite3.connect("file:/data/rag.db?mode=ro", uri=True)
result = db.execute("PRAGMA integrity_check").fetchone()[0]
assert result == "ok", result
print("restored rag.db integrity_check: ok")
'
created=false
echo "restored $backup into volume $volume in $(( $(date +%s) - started ))s" >&2
