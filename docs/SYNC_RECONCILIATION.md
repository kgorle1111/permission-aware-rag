# Durable document reconciliation

Permission reconciliation records the desired document changes before writing to
Qdrant. This lets a retry repair a partial remote write without hydrating every
chunk policy or replaying every document's grants. Retrieval remains blocked
until the SQL mirror, provider cursor, and vector payloads have completed the
same reconciliation intent.

The implementation is in [platform/app/sync.py](../platform/app/sync.py), with
journal models in [platform/app/store.py](../platform/app/store.py). These are
recovery records for permission updates to already indexed content. They do not
store or restore document text.

## Three committed phases

Every phase uses `permission_transaction`, which locks the shared
`PermissionState` row. Retrieval, generation, auditing, sync, and full ingestion
use the same locking boundary. Revision checks prevent an older reconciliation
from completing over a newer mutation.

| Phase | Work | Durable state after commit |
|---|---|---|
| 1: block reads | Increment the revision and mark reconciliation pending, before parsing the source or touching Qdrant. | The pending barrier survives subsequent parsing, remote, and SQL failures. |
| 2: record intent | Validate the mirror and source; merge existing document intents with the latest patch; inspect affected policies; record changed or previously pending documents. Save a candidate Drive cursor separately. | `PermissionMutation` holds desired grants. `PendingSourceCheckpoint` holds an uncommitted provider cursor. Deletion intent also sets `rebuild_required` before any remote deletion. |
| 3: apply and complete | Recheck the revision; apply journaled vector deletions and document ACL writes; update affected SQL rows; validate and promote pending cursors. | One SQL commit updates the mirror and committed cursor, clears both journals, advances the revision, and clears the pending/rebuild barriers. |

Qdrant and SQL do not share an atomic transaction. If a remote call succeeds but
its acknowledgement is lost, or the final SQL commit fails, the phase-2 journal
and barrier remain committed. Reapplying its document-filtered ACL writes is
idempotent. Cache clearing follows successful completion; permission revisions
also prevent an older cache entry from satisfying a later request.

## Patch and retry semantics

JSONL is a patch feed. An omitted document leaves its stored permissions
unchanged unless an earlier failed reconciliation already journaled an intent
for that document. Such an intent survives omission in the next patch. An
explicit later patch for the same document replaces its saved desired grants.
A journal `null` denotes deletion; `[]` denotes denial. Malformed ACLs become an
empty grant list, which denies everyone. Invalid source
structure or document IDs fail reconciliation and leave reads blocked.

For example, a salary-document revocation may reach Qdrant before SQL commit
fails. If the next patch mentions only an engineering document, reconciliation
still applies both the saved salary revocation and the new engineering change.
It cannot silently restore the salary document's old SQL grants.

Document changes modify only `acl_doc` in Qdrant. Section and paragraph grants
remain independent, and retrieval still requires the caller to satisfy all
three levels before ranking. SQL retains those child policies and recomputes its
conservative flat metadata for affected chunks.

Drive always reads from the committed `SourceCheckpoint`, not a pending cursor.
A cursor advances only with successful application of all saved and latest
intents. Full replacement clears the committed Drive cursor so the next Drive
pass takes a fresh baseline.

## Deletion and replacement

A deletion intent commits `rebuild_required` before removing vector points.
Successful deletion completes normally. A failure after intent persistence leaves
ordinary sync blocked: a later grant cannot recreate missing document content
or clear that rebuild requirement. Recovery requires successful full reingestion.

[Full ingestion](../platform/app/ingest.py) first commits its own pending/rebuild
barrier. Its successful replacement transaction writes the new SQL mirror and
clears document intents, pending cursors, and committed provider cursors together.
A failed replacement keeps reads blocked and does not discard the old journals.

## Upgrading an index created before journals

A prior release could finish a Qdrant grant write and lose its acknowledgement
before the SQL mirror committed. Its global pending barrier contains no
per-document recovery intent. Creating empty journal tables and accepting an
empty patch would then unblock retrieval while the remote grants differ from SQL.

Startup checks for a pending existing permission state when either journal table
is absent. It commits `rebuild_required` and a revision increment **before** schema
creation, so a crash after table creation cannot erase the upgrade barrier.
Ordinary sync cannot clear it: full successful reingestion is required. An already
committed rebuild intent remains unchanged; clean pre-journal indexes upgrade
without a forced rebuild, and current journaled retries retain their normal path.
Stop other application writers during schema upgrades; concurrent old/new
versions sharing the mirror are not a supported migration procedure.

[Eight upgrade regressions](../platform/tests/test_sync_journal_upgrade.py)
verify an actual remote public grant followed by SQL rollback, absence of either
journal table, crash during bootstrap, clean upgrade, current retry recovery,
revision idempotence and active-row scope. The disclosure regression failed on
the previous bootstrap before the guard was added. The complete platform suite
passed 366 tests at 98.97% combined branch-aware coverage.

## Scope and verification

The optimization reduces full ORM hydration and all-document vector replay.
Indexed lookups hydrate only affected chunk policies; SQL updates use bulk
mappings, and equal document grants share batched vector writes. Necessary global
SQL existence and aggregation checks remain. They detect missing policy rows,
SQL or JSON-null paragraph policies, and inconsistent document grants, including
corruption outside the latest patch. Drive also obtains distinct known document
IDs. These operations can still scale with the mirror; this change does not
eliminate every linear SQL check.

The SQL mirror and permission sources are trusted inputs within the stated
application boundary. These checks do not guarantee detection of arbitrary
administrator tampering, every malformed policy, or unauthorized direct writes
that bypass the shared mutation protocol. The runtime's illustrative PostgreSQL
RLS example is not an activated end-user authorization layer.

[Security regressions](../platform/tests/test_sync_journal_security.py) exercise
real embedded Qdrant writes followed by remote exceptions or SQL commit failures,
omitted/malformed follow-up patches, explicit overrides, revision supersession,
Drive cursors, deletion/regrant attempts, failed replacement, and journal cleanup.
Four retry cases failed against the previous implementation before the journal
was added. Two further tests exposed JSON-null and unrelated-policy validation
gaps before those checks were repaired. All 19 cases passed against the reviewed
implementation. [Scale regressions](../platform/tests/test_sync_scale.py) separately
check scoped hydration, replay across connection-pool recreation, and actual
payload-write counts.

Run the focused checks from `platform/`:

```bash
python -m pytest tests/test_sync_journal_security.py tests/test_sync_scale.py -q
```

Focused tests establish these recovery properties, not the complete mutation
gate or a large-scale latency result. After the upgrade guard, the complete mutation report validated
1,677/1,693 killed with 16 reviewed equivalents. Unchanged-module results were
retained; new bootstrap mutants ran serially and actionable survivors gained
regressions. The strict source/mutant-hash validator passed.
The [optimized 100k-document report](../evals/results/2026-10-08-sync-optimized/table.md)
records the separate latency measurement and preserves the baseline miss.
