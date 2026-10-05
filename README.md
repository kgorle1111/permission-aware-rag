# Permission-Aware RAG

A small reference application for JWT-authenticated, permission-filtered retrieval. It combines a FastAPI API, Qdrant vector search, SQL-backed permission state and audit records, local embeddings by default, and either a synthetic JSONL feed or an optional Google Drive Changes API backend. It is intended for local evaluation and development; it is not a validated production deployment.

## Run locally

Requires Python 3.11 or 3.12. See [SETUP.md](SETUP.md) for setup, configuration, sync, and migration notes.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/gen_keys.py
python scripts/ingest.py
DEMO_MODE=1 uvicorn app.main:app --port 8090
```

The demo UI is at <http://localhost:8090>. In another terminal, `python scripts/demo.py` runs the scripted example against the running API. `DEMO_MODE=1` enables development token minting and must not be used for a real deployment. See [SETUP.md](SETUP.md) for test, coverage, and mutation-check commands.

For a containerized local run, use `docker compose up --build`. The container initializes the corpus and signing keypair on first start and stores SQL, Qdrant data, and runtime keys in the persistent `rag-data` volume mounted at `/data`. Demo token minting remains off unless you explicitly set `DEMO_MODE=1`. See [SETUP.md](SETUP.md) for the volume lifecycle and public-key-only configuration.

## Current behavior and boundaries

- Requests authenticate with RS256 JWTs. Retrieval applies the ACL predicate in the Qdrant query and constructs the answer from returned chunks. Access checks, generation, and the success audit write are performed inside the permission-state transaction/locking boundary. If reconciliation is marked pending or retrieval fails, the request returns the empty response.
- Document ACLs are narrowed by optional section ACLs using set intersection (“strictest” policy). This is conservative: users authorized by only one overlapping membership can lose access (a false negative). It does not implement hierarchical or relationship-based authorization.
- `corpus/permissions.jsonl` is a patch feed: a missing document leaves its stored permissions unchanged. A row with `{"doc_id":"...","deleted":true}` explicitly deletes that document. Set `PERMISSIONS_BACKEND=gdrive` to use Drive snapshots and changes instead. Drive file IDs must match the ingested corpus `doc_id` values; the service account needs Drive metadata read access, and Drive email/domain permissions must map to the signed JWT `sub` and `groups` claims. A failed reconciliation keeps reads blocked until a successful reconciliation repairs state.
- Drive's change page token is saved in SQL in the same transaction as ACL updates. An initial sync snapshots the permissions of known ingested file IDs. Reingestion clears the saved token and causes the next sync to take a fresh baseline. An optional webhook at `/webhooks/drive` accepts notifications only when the configured channel ID, channel token, and resource ID all match; polling remains enabled as the recovery path.
- Permission sync updates access for already indexed content; it does not fetch or restore document contents. Reingest after changing the permission source/backend or moving to a different physical SQL/vector store. After a document is deleted from the index, restoring its access requires reingesting its content.
- The API polls the configured feed every `SYNC_INTERVAL_S` seconds (default `10`; set `0` to disable). `POST /sync`, restricted to the `security` group, runs a pass. Do not run the standalone sync script concurrently with a live API: it does not share the API's in-process vector-store lifecycle. Use `POST /sync` for a live process.
- Cache entries are bounded and keyed by permission revision, principal scope, exact query, and `k`. They are local to a process; the SQL revision is used to make stale entries miss after reconciliation.
- `denied_count` is a count sampled from the unfiltered top-k vector results for that query. It is not a count of every forbidden chunk in the corpus and is not a comprehensive exposure metric.
- `config/postgres_rls.sql` is an example policy and is not active end-user authorization in the runtime. Runtime authorization comes from the application's verified principal and ACL checks.

The optional benchmark report in [reports/benchmark.json](reports/benchmark.json) records a run against the small offline fixture. Reproduce it with `python scripts/benchmark.py --iterations 20 --output reports/benchmark.json` and inspect the generated JSON for the run's environment, sample count, and results. Measurements are local observations, not production capacity or a security proof.

The tests exercise local code paths and fakes. They do not establish resistance to every prompt injection or side channel, nor validate a live Google account, external Qdrant server, PostgreSQL deployment/RLS, identity provider, or LLM integration. Consult [SECURITY.md](SECURITY.md) before deployment.

## Repository map

`app/` contains the API, identity verification, retrieval, ingestion, synchronization, SQL store, and connector adapter. `scripts/` contains local setup and demo commands. `corpus/` contains demo documents and the synthetic permission feed. `tests/` contains the offline test suite. `config/postgres_rls.sql` is an optional example, not an activated runtime control.
