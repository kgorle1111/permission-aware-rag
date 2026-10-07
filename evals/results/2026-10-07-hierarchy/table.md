# Hierarchical permissions and scale prerequisites — 2026-10-07

Local Python 3.12 verification of the document → section → paragraph change.
Permission checks require a match at every level before ranking. The frozen
210-document corpus and probe hash remain unchanged; it has flat permissions.

| Check | Measured result |
|---|---|
| Reference tests, including real PostgreSQL and populated legacy migration | 58 passed |
| Label gate | 14/14 recall@4; zero leaks |
| Isolation oracle | 0/124 differences, including 8 explicit hierarchy probes |
| Planted leak bugs | 8/8 caught; includes OR-level widening and flattened intersection |
| Frozen in-memory evaluation | 0/1350 leaks; 1170/1170 recall@4 |
| Frozen pgvector/RLS evaluation | 0/1350 leaks; 1166/1170 recall@4 |
| Platform tests | 322 passed; 98.91% combined branch-aware coverage, 96% floor |
| Platform mutation campaign | 1471/1486 killed; 15 reviewed hash-pinned equivalents; zero unresolved results |
| Remote Qdrant 1.18.0 / client 1.18.0 | Hierarchy, document bulk revocation, preserved child restrictions, four keyword indexes, on-disk payload/vectors and int8 settings verified |

The frozen evaluation uses `db6b5dcb51a5fb1d6846986cd9d5a610aeae639242012c01530ce7116bf2a837`.
Wilson 95% leak-rate upper bound is 0.28% on each backend; recall intervals are
99.7%–100.0% (memory) and 99.1%–99.9% (pgvector). These template-derived probes
share accounts and a retriever. The bound applies to this synthetic distribution,
and does not quantify hierarchical or arbitrary-corpus safety. The eight new
hierarchy probes are deterministic regressions, not a large independent sample.

The complete campaign plus targeted survivor reruns passed the strict mutation
validator. Eight new regression tests killed 27 meaningful survivors. Two macOS
fork aborts were rerun with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` and failed
actual assertions; they were not counted as kills while unresolved. Equivalent
entries were reviewed against the current source and mutant hashes.

## Reproduce

From `app/`, run `python -m pytest -q` with `DATABASE_URL` pointing to a temporary
PostgreSQL/pgvector database, then `python run_evals.py`,
`python run_evals.py --mutants`, and `python eval_scale.py`.
For `python eval_scale.py --pgvector`, use a fresh empty disposable database.
The migration test requires an administrative test DSN and creates/drops its own database.

From `platform/`, run the branch-coverage and mutation commands in
[AGENTS.md](../../../AGENTS.md). Against a disposable local Qdrant server, run
`python ../evals/verify_qdrant_storage.py --url http://127.0.0.1:6333`.
The diagnostic creates and deletes only its uniquely named synthetic collection.

## Limits

- Storage settings and engine ACL behavior were checked on a real server. Embedded
  Qdrant ignores indexes/quantization. Compressed resident memory, ANN recall,
  100k-document latency and 10k-document revocation targets were not measured.
- Bulk ACL updates reduce vector writes by grouping document IDs with equal grants;
  permission reconciliation still scans the SQL mirror. Failure leaves a durable
  pending barrier; interrupted deletion requires full reingestion.
- Legacy SQLite policy stores require reingestion rather than silently granting
  access. PostgreSQL upgrades preserve legacy document ACLs with public child levels.
- The separate PostgreSQL ingestion-role fix remains open (T13); these ACL changes
  do not protect against an attacker who can forge GUCs through arbitrary SQL.
- The live AWS showcase runs the prior stable reference image, not this new batch.
