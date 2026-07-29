# Setup — from clone to live

Everything in the repo is ready; the steps below are the parts that need **your**
accounts or **your** data. Ordered from 2 minutes to production.

---

## 1. Run locally (0 accounts, 0 dependencies)

```bash
git clone https://github.com/kgorle1111/permission-aware-rag && cd permission-aware-rag/app
python3 run_evals.py                # proof first: 20 cases | recall@4 14/14 | leaks 0
python3 underwriter_server.py 8421  # open http://127.0.0.1:8421
```

## 2. Deploy the public demo (Render free tier, ~3 minutes, $0)

The repo contains a [`render.yaml`](render.yaml) blueprint — Render reads it and
configures everything (free plan, Python 3.12, `HOST=0.0.0.0`, start command).

1. Go to **https://dashboard.render.com** → sign in with GitHub (create the account if
   needed — this is the one step that requires you).
2. Click **New → Blueprint**, select the **`permission-aware-rag`** repo, click
   **Deploy**. That's it.
3. Your URL will be `https://permission-aware-rag.onrender.com` (or similar — Render
   shows it on the service page).

Notes for the free tier:
- The instance **sleeps after ~15 min idle**; the first visit after a sleep takes
  ~30–60 s to wake. Fine for recruiters; put "(free tier — first load may take 30 s)"
  next to the link.
- The demo deploys **retrieval-only** (no LLM key, so zero spend and nothing to abuse).
- The audit log is ephemeral (resets on redeploy/sleep) — expected for a demo.

**Then:** paste the live URL into the README's *Try it in 60 seconds* section (or ask
Claude to — "add the live demo link <url> to the README").

## 3. Enable drafted answers on the demo (optional, ~$)

In the Render dashboard → your service → **Environment** → add
`ANTHROPIC_API_KEY = sk-ant-…` (create a key at https://console.anthropic.com with a
**spend limit** — $5 is plenty).

Cost math: Haiku 4.5 at ~2k tokens in / 300 out ≈ **$0.002 per question**, and the app
rate-limits `/ask` to 10/min/IP. A hostile visitor hammering it all day costs ~$3.
Every response carries `est_cost_usd`, and `/audit` shows the running total.

## 4. pgvector backend (optional, for the "production posture" story)

Locally with Docker:

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg16
pip install "psycopg[binary]"
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
python3 -c "from app.pgvector_rag import setup_schema; import os; setup_schema(os.environ['DATABASE_URL'])"
RAG_BACKEND=pgvector DATABASE_URL=postgresql://rag_app:rag_app@localhost:5432/postgres \
  python3 app/underwriter_server.py 8421
```

On Render: add a **Render Postgres** (free 30-day / paid after), run `setup_schema`
against it once, then set `RAG_BACKEND=pgvector` + `DATABASE_URL` (the `rag_app` DSN) on
the web service. Note: Render's managed Postgres includes pgvector.

## 5. Real production (when a real shop pilots it)

These are the seams the code already has — each is a config change plus your data:

| Step | What you do | Where it plugs in |
|---|---|---|
| **Identity from SSO** | Issue HS256 JWTs from your IdP (or ask Claude to add RS256/JWKS validation for Okta/Entra) with `sub` + `groups` claims | `UNDERWRITER_JWT_SECRET` env var — `can_read()` unchanged |
| **Real documents** | Nightly export per source system → one `add_document(doc_id, text, acl)` call each, ACLs copied from the system of record | `CORPUS` in `underwriter_server.py` → replace with your sync job (`INTEGRATION.md` has the source→ACL mapping table) |
| **Real guidelines manual** | Paste ≥4,096 tokens of your underwriting manual into `SYSTEM_PROMPT` | `app/llm.py` — prompt caching engages automatically (verify via `cache_read_input_tokens`) |
| **Semantic retrieval** | Get a Voyage AI (or run sentence-transformers) key and swap `embed()` | `app/embedding.py` — one function; RLS/ACL logic untouched |
| **TLS + real host** | Any reverse proxy (Render/Fly provide TLS automatically) | — |
| **Hide the demo side channel** | `SHOW_DENIED=0` | env var |
| **Answer-level evals** | Hand-label ~20 Q/A pairs per role before iterating on the prompt | extend `evals.json` / `run_evals.py` |

## What's already done (no action needed)

Tests + 20-case leak gate + lint in CI on every push · ACL pre-filtering with
visible-set statistics · pgvector/RLS backend · prompt-injection boundary · citation
verification · hash-chained audit with CSV export · JWT seam · rate limiting, input
caps, CSP/nosniff · cost/latency receipts · dark/light UI with keyboard-first UX.
