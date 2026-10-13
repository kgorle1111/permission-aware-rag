# Contributor setup

How to get each part of this repository running and how to pass the same gates CI runs.
There are three independent parts. You only need the one you're changing.

| Part | Where | Needs |
|---|---|---|
| Reference app (in-memory backend) | `app/` | Python 3.12, nothing else (stdlib only) |
| pgvector backend (Postgres RLS) | `app/pgvector_rag.py` | Python 3.12, `psycopg`, Postgres 16 with pgvector |
| Production platform | `platform/` | Python 3.11 or 3.12, its own `requirements*.txt` |

## 1. Reference app

```bash
cd app
python3 run_evals.py                # leak gate: exits 1 on any ACL leak or recall miss
python3 underwriter_server.py 8421  # UI at http://127.0.0.1:8421
```

`ANTHROPIC_API_KEY` in your environment enables drafted answers. Without it the app answers
retrieval-only. Never commit a key; `.env*` files are gitignored.

Gates, as CI runs them (`.github/workflows/ci.yml`):

```bash
pip install pytest ruff
ruff check . && ruff format --check .       # from the repo root
cd app
pytest -q
python run_evals.py
python run_evals.py --mutants               # every planted leak bug must be caught
python ../evals/ladder.py --check           # every leaking negative-control rung must be rejected
```

## 2. pgvector backend

Start a throwaway Postgres with pgvector, then run the RLS gates against it:

```bash
docker run -d --name pgv -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg16
pip install "psycopg[binary]" pytest
cd app
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest test_pgvector.py -q
```

The scaled eval needs an empty database:

```bash
python -c "import psycopg; psycopg.connect('postgresql://postgres:postgres@localhost:5432/postgres', autocommit=True).execute('CREATE DATABASE scale_eval')"
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/scale_eval python eval_scale.py --pgvector
```

To run the server on this backend, see "Two backends, one guarantee" in
[README-technical.md](README-technical.md). It covers `RAG_BACKEND`, `DATABASE_URL`,
`INGEST_DATABASE_URL`, `RAG_PRINCIPAL_KEY` and the audit checkpoint.

## 3. Platform

```bash
cd platform
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/ -q --cov=app --cov=scripts --cov-branch --cov-fail-under=96
```

Always pass `tests/`. A bare `pytest` also collects the gitignored `mutants/` copy and fails.
Running the server locally and in Docker is covered in [platform/README.md](platform/README.md).

### The mutation gate

CI runs mutmut over `platform/app` with one worker and fails on any surviving mutant that is
not a reviewed, hash-pinned equivalent in `platform/mutation_equivalents.json`:

```bash
mutmut run --max-children 1 && mutmut export-cicd-stats
python scripts/check_mutations.py mutants/mutmut-cicd-stats.json --mutants-dir mutants \
  --equivalents mutation_equivalents.json --source-root .
```

- A full run takes about 75 minutes. Locally, more workers find survivors faster, but parallel
  workers share test state and report false kills. Trust their survivor list, not their kills.
- Any edit to a module changes its file hash and invalidates that module's pins. Re-pin only
  after checking that each pinned mutation still matches its written reason.
- Kill a survivor with a test when behaviour can tell it apart. Pin it only when no input can.
- **Never run a formatter over `platform/`.** Root ruff excludes it, but naming platform paths on
  the command line still reformats them and breaks every pin.

## Conventions

- **Ledgers:** every threat (`docs/THREAT_MODEL.md`, `T<n>`) and backlog item (`ROADMAP.md`,
  `B<n>`) row cites a test or code anchor, and `app/test_ledgers.py` checks that each anchor exists.
  Take the next free id; never reuse one.
- **Shortcuts:** a deliberate shortcut in code carries a `kn:` or `ponytail:` comment and a ROADMAP row.
- **Tests first:** a fix lands with a test that failed before it.
- **Commits and branches:** conventional commits that say why, one branch per concern, PRs into `main`.
- **Paid model calls:** CI never calls a paid model. The live contract test
  (`.github/workflows/live-contract.yml`) runs weekly or on demand, and only with the
  `ANTHROPIC_API_KEY` repository secret.
