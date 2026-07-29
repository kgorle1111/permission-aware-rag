# Improvement Backlog — sweep of 2026-07-20

Produced by the improvement-sweep pipeline (5 category lanes × ~100 ideas → top 10
each, verified against code) + an independent security review. Nothing here is
implemented yet — pick, then we execute per the file-partition rule.

## ⚠ Security review findings (fix before/with any wave)

| # | Sev | Finding | Fix sketch |
|---|-----|---------|-----------|
| S1 | HIGH | ~~**IDF side channel:** ACL-hidden docs measurably shift visible scores.~~ **FIXED 2026-07-28:** df/n now computed over the caller-visible set at query time; regression test asserts identical scores with/without a hidden doc. | — |
| S2 | HIGH | ~~**/audit leaks cross-user:** any role reads every user's queries.~~ **FIXED 2026-07-28:** `/audit` requires `user`, entries filtered to the caller; full view gated to the `audit` group (auditor role). | — |
| S3 | MED | **Prompt injection:** doc text spliced into the LLM prompt with no data/instruction boundary — dangerous once docs come from source-system pulls. | Delimit chunks as data + system-prompt rule + injection leak test. |
| S4 | LOW | demo_server renders doc text via unescaped innerHTML (workbench ui.html escapes correctly). | Mirror `esc()` or use textContent. |
| S5 | LOW | /ask = unthrottled paid API call (bounded by localhost today). | Per-IP token bucket before binding beyond loopback. |

Clean: no secrets in tree or git history; `can_read` logic sound; empty-ACL refusal works; ui.html escaping correct.

## Category lanes (top 10 each, deduped — "also:" marks cross-lane convergence)

### Design / UX (`app/ui.html` unless noted)
1. Clickable citations — `[doc-id]` in the answer scrolls to + flashes the source card; one-click claim verification. *(also Ease#7)* — S
2. Copy-to-file-note button — answer + citations + timestamp + role to clipboard; the primary "answers out" path has no copy affordance today. *(also Ease#2)* — S
3. Deep links — read `?q=&user=` on load, auto-submit, `replaceState` after each ask; INTEGRATION.md promises this, UI doesn't do it. *(also Ease#1,#3)* — S
4. Role-switch comparison banner — "as senior: 4 sources (was 2 as junior)"; makes the permission model legible. — M
5. Query-term highlighting (`<mark>`) in source excerpts. — S
6. Explain the "hidden by permissions" badge — which data classes this role can't see + who to escalate to. — S
7. Escalation hint on empty results when denied_chunks > 0 ("N docs exist your role can't access — route to senior"). — S
8. Audit trail timestamps + status (ts is logged but dropped by the UI). — S
9. "Draft findings — verify before acting" framing on the answer card; the product stance made visible. — S
10. Recent-questions history chips (localStorage, last ~10 with role). *(also Ease#4)* — S/M

### Technical depth
1. Persist audit log to JSONL (in-memory list dies on restart — defeats the compliance story). *(also Sec-lane hash-chain variant)* — `permission_rag.py`, `underwriter_server.py` — S
2. 20-case eval harness — per-role Q/A with expected-doc AND must-not-cite assertions; recall@k + leak-rate runner. *(also Quality#1; prereq for #3)* — new `app/evals.json`, `app/run_evals.py` — M
3. BM25 scoring (term saturation + length normalization, ~15 lines, ACL untouched) — verify with #2. — `permission_rag.py` — S
4. `ThreadingHTTPServer` — one slow 60s LLM call currently blocks every other user. — both servers — S
5. Skip LLM call on zero retrieval results (deterministic guard; saves a paid call). — `underwriter_server.py` — S
6. Post-hoc citation verification — flag `[doc-id]`s not in the retrieved set (`unverified_citations` in response); the aggregation-risk mitigation is unverified today. — `llm.py` — S
7. Read Anthropic error bodies + retry once on 429/529 (bare exception string swallows the real error). — `llm.py` — S
8. `remove_document` / re-ingest with df correction (nightly re-sync currently duplicates chunks + skews IDF). *(also Quality#5 dup-guard)* — `permission_rag.py` — M
9. Sentence-boundary chunking with overlap (80-word hard cuts split facts mid-sentence). — `permission_rag.py` — M
10. Cost/latency observability per /ask (wall time, tokens, cache hit rate, running spend in audit + `/audit` summary) — makes value receipts measurable. — `underwriter_server.py`, `llm.py` — S

### Ease of use / workflow fit
1. Deep-link query params *(= UX#3)* — S
2. Copy-as-file-note *(= UX#2)* — S
3. Shareable result URLs *(merged into UX#3)* — S
4. Recent-questions history *(= UX#10)* — S
5. Keyboard shortcuts — `/` focus, `1–4` roles, `Esc` clear; target user is keyboard-heavy. — `ui.html` — S
6. Presets from `presets.json` served at `/presets` — shops edit JSON, not HTML; makes the "adjust to any workflow" claim safe for non-devs. — server + ui + new file — M
7. Clickable citations *(= UX#1)* — S
8. Audit trail export (CSV button or `/audit?format=csv`) — compliance needs to take the trail out; currently screenshot-only. — S
9. Retrieval-only quick mode — "Sources only (instant)" checkbox hitting `/query`; skips the LLM for plain lookups. — `ui.html` — S
10. Copy-as-curl link per query — makes the API-embed tier self-documenting for shop IT. — `ui.html` — S

### Security hardening
1. Lock down `/audit` *(= review S2)* — S
2. Prompt-injection boundary + leak test *(= review S3)* — S
3. Demo XSS fix + CSP/nosniff headers on both servers *(⊃ review S4)* — S
4. Input caps — q length ~1000 chars, bound k, clean 400s. — both servers — S
5. Per-IP rate limit on /ask + audit list cap *(= review S5)* — S
6. Sanitize LLM error leakage to browser (log server-side, return generic note). — `underwriter_server.py` — S
7. Tamper-evident audit log — JSONL + SHA256 hash chain *(superset of Tech#1)* — M
8. `SHOW_DENIED` config switch — the "N hidden" count is a deliberate demo side channel; default off outside demo. — servers + ui — S
9. POST for /ask & /query before real data (PII in GET strings → browser history/proxy logs). — server + ui — M
10. SSO/JWT identity seam — `resolve_user(request)` that validates a JWT when configured, dropdown otherwise; `can_read()` unchanged. — `underwriter_server.py` — M

### Quality
1. LLM answer eval harness *(= Tech#2)* — M
2. `test_llm.py` with mocked urlopen (payload shape, cache_control, no-key path, usage passthrough — zero coverage today). — S
3. HTTP endpoint tests via `http.client` (400 paths, /audit shape, LLM-fallback branch). — M
4. Chunking/boundary tests for `add_document` (multi-chunk, id numbering, IDF drift). — S
5. Duplicate `doc_id` ingest guard + test (ValueError like the empty-ACL guard). — S
6. Single test runner + GitHub Actions CI on 3.12. — S
7. Type hints everywhere + ruff config in `pyproject.toml` (house standard). — S
8. Defensive parsing of malformed API responses in `llm.ask`. *(pairs with Tech#7)* — S
9. Audit bounding (`deque(maxlen=1000)`) + `elapsed_ms` per retrieve (value-receipt feed). — S
10. README/docs accuracy pass (ports, k=4 default, audit schema, eval commands). — S

## Recommended first wave (if asked)

**DONE 2026-07-28 (wave 1):** S1, S2, deep links, copy-to-file-note, clickable
citations, eval harness (20 cases, recall@4 14/14, 0 leaks), audit persistence
(JSONL), ThreadingHTTPServer.

**DONE 2026-07-28 (wave 2):** S3 (injection boundary + mocked leak test),
S4 (demo XSS + CSP/nosniff on both servers), S5 (per-IP rate limit on /ask),
input caps, skip-LLM-on-zero-results, post-hoc citation verification
(`unverified_citations` + UI badge), API error bodies + retry on 429/529,
sanitized LLM errors to browser, `test_llm.py` (mocked urlopen).

**DONE 2026-07-28 (wave 3):** BM25 scoring (eval-verified: recall@4 14/14,
0 leaks, ACL untouched), duplicate-ingest guard, chunking tests, HTTP endpoint
tests (`test_http.py`: 400s, /audit scoping, rate limit, skip-LLM), audit
`elapsed_ms` + in-memory bound (1000), GitHub Actions CI with leak gate.

**DONE 2026-07-28 (wave 4):** keyboard shortcuts (/, 1–4, Esc), query-term
highlighting, audit timestamps + elapsed_ms in UI, retrieval-only quick mode,
escalation hint on empty+denied, audit CSV export (`/audit?format=csv`),
defensive parsing of malformed API responses.

## Execution notes (from the pipeline doc)

Partition by FILE OWNERSHIP when implementing: `permission_rag.py` and
`underwriter_server.py` are contested — one serial worker. New-file items (evals,
tests, CI, presets.json) parallelize freely. `ui.html` gets its own dedicated pass
after backend items land.
