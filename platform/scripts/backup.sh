#!/usr/bin/env bash
# Back up the platform's /data volume into OUTPUT_DIR/<UTC timestamp>/.
#
#   scripts/backup.sh <container|volume> <output-dir> [image]
#
# Consistency: rag.db (ACLs, audit log, permission revision, sync token) and
# qdrant_data/ (Qdrant local mode) must come from the same instant, or the
# restored index can carry payload ACLs that the restored SQL state doesn't.
# Qdrant local mode has no snapshot API and isn't safe to copy mid-write, so we
# stop the container for the duration (measured in docs/BACKUP_RESTORE.md) and
# restart it on exit. The SQLite copy still goes through the sqlite3 backup API
# (scripts/sqlite_backup.py) so a hot journal is never copied raw.
# kn: stop-the-world; a live backup needs Qdrant server mode + its snapshot API.
#
# Given a volume, refuses if any running container mounts it.
# Everything else in /data (IdP keys, .corpus-ingested marker) is copied as-is.
# Covers only the default layout: SQLite at /data/rag.db, Qdrant at /data/qdrant_data.
set -euo pipefail
umask 077

usage() { echo "usage: $0 <container|volume> <output-dir> [image]" >&2; exit 2; }
[ $# -ge 2 ] && [ $# -le 3 ] || usage
target=$1
out_root=$2
script_dir=$(cd "$(dirname "$0")" && pwd)

# GNU "hash  path" lines either way; macOS's own /sbin/sha256sum prints BSD tags.
# An array, not a function: xargs can't call shell functions.
if command -v shasum >/dev/null 2>&1; then hasher=(shasum -a 256); else hasher=(sha256sum); fi

container=""
if docker container inspect "$target" >/dev/null 2>&1; then
  container=$target
  volume=$(docker container inspect --format \
    '{{range .Mounts}}{{if and (eq .Destination "/data") (eq .Type "volume")}}{{.Name}}{{end}}{{end}}' "$container")
  [ -n "$volume" ] || { echo "container $container has no named volume at /data; back up its bind mount directly" >&2; exit 1; }
  image=${3:-$(docker container inspect --format '{{.Image}}' "$container")}
elif docker volume inspect "$target" >/dev/null 2>&1; then
  volume=$target
  users=$(docker ps --quiet --filter "volume=$volume")
  if [ -n "$users" ]; then
    echo "volume $volume is mounted by running container(s) $users; pass the container name so it is stopped for a consistent copy" >&2
    exit 1
  fi
  image=${3:-permission-aware-rag:local}
else
  echo "$target is neither a container nor a volume" >&2
  exit 1
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
dest="$out_root/$stamp"
[ ! -e "$dest" ] || { echo "$dest already exists" >&2; exit 1; }
mkdir -p "$dest/data"

was_running=false
restart() {
  if $was_running; then
    docker start "$container" >/dev/null && echo "restarted $container" >&2
  fi
}
trap restart EXIT

started=$(date +%s)
if [ -n "$container" ] && [ "$(docker container inspect --format '{{.State.Running}}' "$container")" = true ]; then
  was_running=true
  echo "stopping $container for a consistent copy" >&2
  docker stop --time 30 "$container" >/dev/null
fi

# Run inside the image (its python, its uid) with the volume read-only; stream a
# tar back so no bind mount or host sqlite3 is needed.
docker run --rm -i --network none --volume "$volume:/data:ro" --entrypoint sh "$image" -c '
  set -eu
  umask 077
  work=$(mktemp -d)
  python - /data/rag.db "$work/rag.db"
  for entry in /data/* /data/.[!.]*; do
    [ -e "$entry" ] || continue
    case "${entry##*/}" in rag.db|rag.db-journal|rag.db-wal|rag.db-shm) continue ;; esac
    cp -a "$entry" "$work/"
  done
  tar -C "$work" -cf - .
' < "$script_dir/sqlite_backup.py" | tar -xf - -C "$dest/data"

restart
was_running=false
elapsed=$(( $(date +%s) - started ))

[ -d "$dest/data/qdrant_data" ] || echo "WARNING: no qdrant_data/ in $volume (QDRANT_URL server mode?); vectors are NOT in this backup" >&2

contains_secrets=no
if [ -n "$(find "$dest/data" -type f -name '*.pem' -print -quit)" ]; then
  contains_secrets=yes
  find "$dest/data" -type f -name '*.pem' -exec chmod 600 {} +
fi

cat > "$dest/backup-info.txt" <<EOF
created_utc=$stamp
source_volume=$volume
source_container=${container:-none}
image=$image
downtime_seconds=$elapsed
contains_secrets=$contains_secrets
EOF

(cd "$dest" && find data backup-info.txt -type f -print0 | LC_ALL=C sort -z | xargs -0 "${hasher[@]}" > MANIFEST.sha256)
chmod 600 "$dest/MANIFEST.sha256" "$dest/backup-info.txt"

if [ "$contains_secrets" = yes ]; then
  cat >&2 <<EOF
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! THIS BACKUP CONTAINS SECRETS: the IdP signing key(s) under data/*.pem. !!
!! Anyone holding idp_private.pem can mint tokens for any user/group.     !!
!! Store it encrypted, restrict access, and never commit or upload it.    !!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
EOF
fi
echo "backup written to $dest (service stopped ${elapsed}s)" >&2
echo "$dest"
