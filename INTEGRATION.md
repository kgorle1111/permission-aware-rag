# Integration Map — fitting the Workbench into an underwriter's existing workflow

Principle: the workbench adapts to the shop's workflow, not the other way around.
Everything below plugs in at one of three seams: **identity in**, **documents in**,
**answers out**.

## 1. Identity in (who is asking)

| Today (demo) | Production |
|---|---|
| Role picker in the sidebar | SSO (Okta / Entra / Google Workspace). Map IdP groups → RAG groups 1:1 (`group:underwriting`, `group:banking`, `group:compliance`, `group:senior`). No per-user ACL editing in this tool — permissions stay owned by IT in the IdP. |

Integration cost: replace the `USERS` dict with a JWT check; `can_read()` is unchanged.

## 2. Documents in (what it can retrieve)

Each source system maps to one `add_document()` call with an ACL copied from the
system of record — never invented here:

| Source system | Data class | ACL |
|---|---|---|
| Policy admin (Guidewire, Duck Creek, AMS360…) | policy status, coverage | `group:underwriting` |
| Claims platform | claims history, subrogation | `group:underwriting` |
| Core banking / financial spreading (nCino, custom) | balances, NSF, utilization | `group:banking` |
| Credit committee memos | DSCR, collateral recommendations | `group:senior` |
| Compliance case tool | watchlist, fraud flags | `group:compliance` |
| Underwriting guidelines manual | rules, thresholds | `*` (public internal) |

Sync pattern: nightly batch pull per system → `add_document(doc_id, text, acl)`.
Start with read-only exports (CSV/PDF-to-text); no write access to any source system, ever.

## 3. Answers out (where results land)

Underwriters live in their policy admin system and email — not in new tabs. Three
integration tiers, cheapest first:

1. **Standalone tab (today):** the workbench UI. Zero integration work.
2. **Deep link:** `/?q=<question>&policy=<id>` from the policy admin's notes field —
   one URL template pasted into the existing system's custom-link config.
3. **API embed:** `GET /ask?user=<role>&q=<q>` returns JSON — drop the drafted findings
   into the underwriting file note via the policy admin's API. The answer text is
   already citation-formatted for paste-into-file-note.

## 4. Workflow presets (sidebar) — mapped to real underwriter tasks

| Preset | Underwriter workflow step |
|---|---|
| New submission review | Guideline check before triage |
| Renewal check | Renewal queue — what's blocking |
| Claims history pull | Loss-run review |
| Bind decision prep | Authority-limit check before binding |
| Compliance screen | Pre-bind clearance |

Presets are one array in `app/ui.html` — each shop edits them to match its own queue
names in minutes. That's the "adjusts to any workflow" mechanism: configuration, not code.

## 5. What is deliberately NOT integrated

- No write-back that changes policy status anywhere (human decides, human types the decision).
- No email/notification sending from this tool.
- No per-query cost above ~$0.01 (Haiku + cached guidelines) — no budget approval seam needed.
