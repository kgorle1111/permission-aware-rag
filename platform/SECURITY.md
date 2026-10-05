# Security notes

This repository is a local reference implementation for permission-aware retrieval. It has not undergone a security review and is not a production-ready security boundary. The local tests exercise selected code paths; they do not prove jailbreak resistance, eliminate all side channels, or validate external integrations.

## Implemented controls

- The API verifies RS256 JWTs and derives user/group principals from verified claims. Demo token minting is available only when `DEMO_MODE=1`; do not enable it in a deployment.
- Qdrant search includes an ACL payload filter. Returned chunks are the context supplied to answer generation.
- Document ACL and section ACL are intersected. This strictest-set policy can produce false denials where a user qualifies through one overlapping membership but not the other.
- SQL permission state is locked across retrieval, generation, and the success audit write. Reconciliation marks permission state pending before applying changes; retrieval fails closed while pending. Reconciliation failure must be repaired with a successful pass.
- The answer cache is bounded and scoped by permission revision, principal scope, exact query, and requested result count. The cache is process-local.
- An optional Google Drive Changes API backend reads file permissions with the metadata read-only scope. Its page token is committed with SQL ACL updates; the mocked tests cover retry and checkpoint rollback behavior. The optional `/webhooks/drive` endpoint requires exact channel ID, secret token, and resource ID matches.

These controls describe the implementation, not a claim that every attack path has been independently audited. In particular, the vector-store implementation also performs an unfiltered top-k query to derive `denied_count`; the local Qdrant library may score results internally. Do not claim forbidden vectors are never scored. The audit field is a top-k sample, not a total count of hidden documents.

## Operator guidance

Use a trusted identity provider and protect signing keys. Never deploy with `DEMO_MODE=1` or use the development private key to issue production tokens. Restrict access to `/audit` and `/sync` at the network layer as well as through the application's `security` group check. Treat the Drive webhook token and channel identifiers as secrets; restrict and protect the mode-600 output file from `scripts/drive_watch.py`. The webhook verifies a channel capability, not an end-user identity. Protect the SQL database, vector index, logs, credentials, and backups as sensitive data.

The JSONL permission feed is synthetic and local. Its rows are patches: absent documents are unchanged; explicit `deleted: true` removes a document. The optional Drive backend requires native Drive file IDs to match ingested `doc_id` values and a trusted mapping from Drive user/group permissions to the API's signed `sub` and `groups` claims. The app can reconcile with a persisted Changes API token and an optional push notification. Keep polling at `SYNC_INTERVAL_S` (default 10 seconds; `0` disables polling) as the recovery path. Webhook channels expire and require manual renewal. Registration with Google and live Drive access have not been tested here. Permission sync changes access for indexed content; it does not retrieve or restore content. Reingest after switching the permission source/backend or physical SQL/vector store. If a sync deletes an indexed document and a later source update grants access again, the missing index is a rebuild condition; restore the content with a full reingestion. A sync failure leaves reads blocked until recovery. Use the live API's `POST /sync` endpoint for manual reconciliation; a standalone sync process is unsupported while the API is live. `/healthz` checks process responsiveness; `/readyz` also checks whether permission state is ready to serve traffic.

The shared SQL permission-state lock serializes retrieval transactions across requests, including answer generation and the success audit write. Slow provider calls inside this critical section can hold up concurrent requests. The local benchmark does not establish throughput for this design or a distributed production deployment; no production scale or availability guarantees are claimed.

Indexes created before `chunk_policy` stored section restrictions require full reingestion. Full reingestion also clears the saved Drive checkpoint; the next pass re-reads a baseline. Reingest after changing the source backend or physical SQL/vector store as well. The local setup uses embedded Qdrant and SQLite. External Qdrant, PostgreSQL, PostgreSQL RLS, identity providers, LLM providers, and live Google Drive access are not validated end-to-end. `config/postgres_rls.sql` is only an example; the runtime does not install a per-request database role/context to enforce end-user RLS.

`reports/benchmark.json` contains the results of the optional offline benchmark on a small synthetic fixture. Generate a fresh report with `python scripts/benchmark.py --iterations 20 --output reports/benchmark.json` and review its environment and probe results. They are local measurements, not a production benchmark or guarantee. The tests are run with `pytest -q`; passing tests are regression coverage for the cases exercised only.

## Reporting a vulnerability

Do not include credentials, private keys, or sensitive customer data in an issue. For a suspected vulnerability, contact the repository maintainers privately through the project's configured security contact. If no private contact is configured, request one without posting exploit details publicly.
