# Backup and restore

The platform keeps all of its state in the `/data` volume (`rag-data` in `platform/compose.yaml`):

| Path | What it holds | If lost |
|---|---|---|
| `rag.db` (SQLite) | ACL source of truth (`chunk_acl`, `chunk_policy`), audit log, permission revision and barrier flags, Drive sync checkpoint, chunk text for the keyword fallback, index fingerprint | Audit history and permission state are gone; nothing rebuilds them |
| `qdrant_data/` (Qdrant local mode) | Vectors plus ACL payloads | Rebuildable by reingest, but only from the source corpus |
| `idp_private.pem`, `idp_public.pem` | Runtime IdP keypair (generated on first start; in `DEMO_MODE` the private key mints tokens) | Every previously issued token stops verifying |
| `.corpus-ingested` | Marker that stops the entrypoint reingesting on start | The next start reingests |

## Back up

```sh
cd platform
scripts/backup.sh <container|volume> <output-dir> [image]
# e.g. scripts/backup.sh platform-rag-1 ~/rag-backups
```

This writes `<output-dir>/<UTC timestamp>/` containing `data/` (a copy of `/data`), `backup-info.txt` (source, image, downtime, `contains_secrets`) and `MANIFEST.sha256` (sha256 of every other file). The last line of stdout is the backup path.

- **The container is stopped while the backup runs, then restarted.** Qdrant local mode has no snapshot API and is not safe to copy mid-write. `rag.db` and `qdrant_data/` also have to come from the same instant: otherwise the restored index can carry ACL payloads that the restored SQL state doesn't have. In the drill below the service was down about 1 s. Downtime grows with the size of `qdrant_data/`; `backup-info.txt` records it on every run. A live backup needs Qdrant server mode and its snapshot API.
- `rag.db` is copied with SQLite's online backup API (`platform/scripts/sqlite_backup.py`, stdlib only, piped into `python` inside the image), and the copy must pass `PRAGMA integrity_check`. The host needs only `docker`, `bash`, `tar` and `shasum` or `sha256sum`. It doesn't need `sqlite3` or Python.
- Given a volume name, it refuses if any running container mounts that volume.
- **If the key files are present, the backup contains secrets.** The script sets them to mode 600, writes `contains_secrets=yes`, and prints a warning. Anyone holding `idp_private.pem` can mint a token for any user and group. Store backups encrypted with restricted access, and never commit or upload them. The rest of the backup is sensitive too: document text, ACLs and the audit trail of who asked what.

## Restore

```sh
scripts/restore.sh <backup-dir> <new-volume> [image]   # image defaults to permission-aware-rag:local
docker run -d --name rag -p 127.0.0.1:8090:8090 -v <new-volume>:/data permission-aware-rag:local
curl -fsS http://127.0.0.1:8090/readyz
```

Before it creates anything, `restore.sh` checks every manifest hash and checks that `data/` contains no files the manifest doesn't list. It refuses on any mismatch, and it refuses to write into a volume that already exists. It then extracts the files into the new volume as root, `chown`s them to the image's `app` user, sets the keys to mode 600, and runs `integrity_check` on the restored `rag.db` as that user. If a step fails, it deletes the volume it created. To point compose at the restored volume, declare it in `compose.yaml` as `external: true` under the name you restored to.

## What is and isn't covered

Covered: the default single-volume layout above, with SQLite at `/data/rag.db` and Qdrant local mode at `/data/qdrant_data`.

Not covered:
- `DATABASE_URL` pointing at PostgreSQL: use `pg_dump` and take it in the same quiet window as the Qdrant copy.
- `QDRANT_URL` (Qdrant server): use Qdrant's snapshot API. `backup.sh` warns when it finds no `qdrant_data/`.
- Secrets outside `/data`: Drive service-account credentials, the `scripts/drive_watch.py` output file, `ANTHROPIC_API_KEY`, and `JWKS_URL` configuration.
- The fastembed embedding cache (`EMBED_CACHE_PATH`, default under `/app`). It is a cache, so a fresh one only means a slower first ingest.
- Application logs, and the image itself. Keep the image tag or digest that each backup was taken with; `backup-info.txt` records it.
- Off-host copies, encryption and retention. These are the operator's job, and the backups themselves need the protection described above.
- **Permission state reverts to backup time.** Grants and revocations made after the backup are missing from the restored SQL until the next reconciliation against the source (JSONL feed or Drive). The drill below did not exercise a revocation made between backup and restore. It is unverified whether startup reconciliation re-applies such a revocation before `/readyz` reports ready, so confirm it with a sync (`POST /sync` as `security`) before serving traffic.

## Migration rollback

`app/store.py:init_db` migrates forward only: `ALTER TABLE chunk_policy ADD COLUMN paragraph_acl JSON`. It also bumps the permission revision and sets `rebuild_required`, so the upgraded service is not ready until a reingest. No down-migration exists.

1. **Before upgrading the image, run `backup.sh`.** That backup is the only rollback point.
2. To roll back: stop the new container, restore the pre-upgrade backup into a **new** volume with the **previous** image (`restore.sh <pre-upgrade-backup> rag-data-rollback <previous-image>`), and start the previous image on that volume. Don't run the previous image against the upgraded volume.
3. Audit rows and permission changes written after the backup are lost from the rolled-back volume. Keep the upgraded volume (don't `docker volume rm` it) until you have exported what you need from it.

## Restore drill (2026-10-21)

Run once, end to end, on throwaway resources only: image `parbk-drill:img`, container `parbk-drill-app`, volumes `parbk-drill-vol` and `parbk-drill-vol-restored`, host port 18391. All of them were removed afterwards. The image was built from `origin/main` at `3802562`; this branch doesn't change `app/`. The scripts came from branch `harden/backup-restore`. Host: macOS with Docker 29.5.2. `$OUT` stands for a scratch directory.

Results:

| Check | Before backup | After deleting the volume and restoring |
|---|---|---|
| `/readyz` | `{"ready":true}` | `{"ready":true}` |
| bob (hr): "what are the salary bands?" | `hr-salaries`, `hr-salaries` (scores 0.424, 0.1125), `source: full_rag` | identical |
| ceo (board): same query | `exec-comp` (0.1068), `source: full_rag` | identical |
| audit rows (`GET /audit` as security, and `count(*)` in SQL) | 2 | 5 = the 2 preserved + 3 new reads |
| permission state `(revision, pending, rebuild_required)` | `(2, 0, 0)` | `(2, 0, 0)` |
| bob's token minted **before** the backup | 200 | 200 (same signing key) |
| sha256 of `rag.db`, `idp_private.pem` and Qdrant `storage.sqlite` in the restored volume | | match the manifest |
| reingest on start | | none (`.corpus-ingested` restored) |
| tampered backup (one byte appended to `rag.db`) | | refused, and no volume created |

**Recovery time: 2 s** from `restore.sh` start to `/readyz` ready (restore alone took 0 s at whole-second resolution). **Backup downtime: 1 s.** Both numbers are for the 8-file demo corpus (`rag.db` is 64 KB). Expect them to grow with `qdrant_data/`.

The first two drill attempts failed, and both failures were fixed before the run above. On macOS, `/sbin/sha256sum` prints BSD-style lines, and `xargs` can't call a shell function, so it ran macOS's `/sbin/sha256` instead. Either way the manifest came out in a format the path check couldn't parse, and `restore.sh` refused the backup. The scripts now pick `shasum -a 256` or `sha256sum` explicitly.

Transcript (container commands and `$OUT` paths shortened; tokens are not printed):

```text
$ date -u
Wed Oct 21 07:13:07 UTC 2026

$ docker run -d --name parbk-drill-app -p 127.0.0.1:18391:8090 -v parbk-drill-vol:/data -e DEMO_MODE=1 parbk-drill:img
{"ready":true}

$ probe BEFORE backup      # /demo/tokens, then POST /query as bob and ceo, GET /audit as secops, SQL counts
bob doc_ids: ['hr-salaries', 'hr-salaries'] scores: [0.424, 0.1125] source: full_rag degraded: False
ceo doc_ids: ['exec-comp'] scores: [0.1068] source: full_rag degraded: False
GET /audit recent rows: 2
sql audit_log rows: 2
permission_state (revision,pending,rebuild_required): (2, 0, 0)
source_checkpoint (provider, token length): []

$ docker exec parbk-drill-app ls -la /data
-rw------- 1 app  app      0 .corpus-ingested
-rw------- 1 app  app   1704 idp_private.pem
-rw------- 1 app  app    451 idp_public.pem
drwx------ 3 app  app   4096 qdrant_data
-rw------- 1 app  app  65536 rag.db

$ scripts/backup.sh parbk-drill-app $OUT
stopping parbk-drill-app for a consistent copy
sqlite backup ok: /data/rag.db -> /tmp/tmp.hE2R1QKeR1/rag.db
restarted parbk-drill-app
!! THIS BACKUP CONTAINS SECRETS: the IdP signing key(s) under data/*.pem. !!
backup written to $OUT/20261021T071309Z (service stopped 1s)

$ cat $OUT/20261021T071309Z/backup-info.txt
created_utc=20261021T071309Z
source_volume=parbk-drill-vol
source_container=parbk-drill-app
image=sha256:4bed3008b94f92c89768e0aa82a193b9456610523c5be708dc018ab1994e75fa
downtime_seconds=1
contains_secrets=yes

$ cut -c67- MANIFEST.sha256         # 8 entries
backup-info.txt
data/.corpus-ingested
data/idp_private.pem                # -rw-------
data/idp_public.pem                 # -rw-------
data/qdrant_data/.lock
data/qdrant_data/collection/chunks/storage.sqlite
data/qdrant_data/meta.json
data/rag.db

$ curl -fsS http://127.0.0.1:18391/readyz        # restarted by backup.sh
{"ready":true}

$ printf x >> <copy>/data/rag.db; scripts/restore.sh <copy> parbk-drill-vol-tamper parbk-drill:img
data/rag.db: FAILED
shasum: WARNING: 1 computed checksum did NOT match
REFUSING: checksum mismatch in <copy>
no tamper volume created

$ docker rm -f parbk-drill-app && docker volume rm parbk-drill-vol
parbk-drill-vol is gone

$ scripts/restore.sh $OUT/20261021T071309Z parbk-drill-vol-restored parbk-drill:img
manifest verified: 8 files
note: backup was taken with image sha256:4bed3008...; restoring for parbk-drill:img
restored rag.db integrity_check: ok
restored $OUT/20261021T071309Z into volume parbk-drill-vol-restored in 0s

$ docker run -d --name parbk-drill-app -p 127.0.0.1:18391:8090 -v parbk-drill-vol-restored:/data -e DEMO_MODE=1 parbk-drill:img
{"ready":true}
recovery seconds (restore.sh start -> /readyz ready): 2

$ docker logs parbk-drill-app | grep -ci ingest
0

$ docker exec parbk-drill-app sha256sum rag.db idp_private.pem qdrant_data/collection/chunks/storage.sqlite   # vs manifest
match: rag.db
match: idp_private.pem
match: qdrant_data/collection/chunks/storage.sqlite

$ probe AFTER restore
bob doc_ids: ['hr-salaries', 'hr-salaries'] scores: [0.424, 0.1125] source: full_rag degraded: False
ceo doc_ids: ['exec-comp'] scores: [0.1068] source: full_rag degraded: False
bob token minted BEFORE the backup, used after restore -> HTTP 200
GET /audit recent rows: 5
sql audit_log rows: 5
permission_state (revision,pending,rebuild_required): (2, 0, 0)
source_checkpoint (provider, token length): []
```

Offline checks live in `platform/tests/test_backup_restore.py`. They cover a byte-identical, openable SQLite copy that includes committed writes and excludes uncommitted ones; refusing a missing source or an existing destination; deleting a copy that fails `integrity_check`; `bash -n` on both scripts; and `restore.sh` refusing a missing manifest or a checksum mismatch before it touches Docker. The Docker drill isn't in CI. Rerun it after any change to the scripts, the `Dockerfile` or the `/data` layout.
