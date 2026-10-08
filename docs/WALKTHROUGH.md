# Five-minute interview walkthrough

A rehearsal script for the permission-aware RAG portfolio. The timings are a
five-minute target; a completed rehearsal or recording is not claimed. Have the
live demo, the current results table and a terminal ready. Pre-run the commands
below so network or environment setup does not consume the interview.

## Preparation

- Open the [live workbench](https://permission-rag-demo.b9hphyfz7skjm.us-east-2.cs.amazonlightsail.com/).
  It runs the stable pre-hierarchy image. Use only synthetic questions.
- Open [current evidence](../evals/results/2026-10-07-hierarchy/table.md),
  [`app/run_evals.py`](../app/run_evals.py), and
  [reviewed mutation equivalents](../platform/mutation_equivalents.json).
- Check out `codex/tier1-hierarchy` for the demonstrated hierarchy commands.
  Prepare `python3 run_evals.py`, `python3 run_evals.py --mutants`, and
  `python3 eval_scale.py` from `app/`. Keep actual outputs, not retyped numbers.
- Open [PR #6](https://github.com/kgorle1111/permission-aware-rag/pull/6).
  At the documented revision `6ea35f04c32c2e802b2d3fbce497fee852f12514`, all
  CI checks succeeded. Recheck before presenting; do not call an open PR merged.

## 0:00–0:40 — State the claim and its scope

**Show:** the workbench landing page.

**Say:** “This is an engineering portfolio about permission-aware retrieval.
The underwriting documents and users are synthetic. The property I test is that
forbidden content cannot alter the reference retriever's results or scores.
The live demo shows role-based sources; it runs an older stable image. I'll use
the tests to show the newer document, section and paragraph permissions.”

Avoid calling demo role selection authentication or the demo a production
insurance deployment.

## 0:40–1:25 — Make permissions visible

**Do:** select **Junior UW**, click **Claims history pull**, and inspect its
sources. Select **Senior UW**, ask “Bank profile Delgado account balance,” then
return to **Junior UW** and ask the same question. The banking document should
appear only for the permitted role; do not promise identical ordering across roles.

**Say:** “The caller's readable chunks are chosen before ranking. In the BM25
reference, corpus statistics are also computed only from those chunks. The demo
is retrieval-only, so this demonstration does not depend on a model following an
instruction to keep secrets.”

**Fallback:** if the endpoint is unavailable, use the repository's workbench
image and run the reference locally. State that the image is recorded evidence;
do not present a failed live request as a successful check.

## 1:25–2:20 — Show why a passing leak test was insufficient

**Show:** the older [planted-fault results](../evals/results/2026-10-03-v2/table.md),
then the actual `run_evals.py --mutants` output.

**Say:** “The original hand-labeled gate caught only one of six deliberately
leaky retrievers. A forbidden document can affect visible scores even when its
ID never appears. I added an isolation oracle: compare IDs and scores against a
corpus containing only readable documents. That caught the original six; the
combined gate now catches eight of eight. The frozen document-ID leak evaluation
caught four of the original six, so I kept both checks and published the misses.”

Explain the distinction between planted retrieval faults and the service's
separate 1,486-mutant campaign.

## 2:20–3:10 — Explain hierarchy with one example

**Show:** `hierarchy_isolation_gate` in [`app/run_evals.py`](../app/run_evals.py).

**Say:** “A document can require HR membership, its section can require Bob's
user ID, and a paragraph can additionally require executive membership. Bob in
HR passes the first two levels; he sees the ordinary paragraph. The restricted
paragraph also requires the third grant. Each level is an AND condition, while
separate principal memberships can satisfy different levels. OR widens access;
flattening principal sets can wrongly deny access.”

Point to the independently authored expected IDs and the result **0/124
isolation differences**, including eight deterministic hierarchy probes. These
are regression cases, not a population-wide confidence interval.

## 3:10–4:05 — Show failure handling and test strength

**Show:** the current results table and mutation equivalents file.

**Say:** “The service keeps a durable permission barrier before updating SQL and
Qdrant. Failure leaves queries blocked until reconciliation succeeds; interrupted
deletion requires reingestion. Its suite has 322 tests and 98.91% branch-aware
coverage. Mutation testing adds evidence beyond coverage: 1,471 of 1,486 mutants
were killed, and 15 were reviewed as behaviorally equivalent and hash-pinned.
They remain survivors in the score; new unexplained survivors fail CI.”

**Show the scale gap:** “The streamed benchmark loaded 100,000 documents and
1.5 million chunks into real Qdrant over HTTP. Search p95 was 4.15 to 8.00 ms
across the measured visibility buckets. But one document's actual `sync_once`
permission update took 102.29 seconds: the SQL mirror scan and replay of all
grants dominate. The harness uses SQLite and streamed ingestion, so I do not
claim production ingest throughput or complete API latency.”

The folder requested 10,000 revocations (9,999 newly changed, one already
revoked), took 104.92 seconds, and missed its 60-second target. Show the
[scale results](../evals/results/2026-10-08-scale/table.md).
Do not turn configured int8 quantization into unmeasured memory or ANN-recall savings.

## 4:05–5:00 — Bound the result and invite reproduction

**Show:** `eval_scale.py` output, the
[public-text kit results](../evals/results/2026-10-08-public-kit/table.md), and their limits.

**Say:** “Both reference backends returned zero forbidden documents in 1,350
frozen synthetic probes. The 95% upper bound is 0.28% on that distribution.
The probes share templates and use flat ACLs, so that number does not prove real
corpus safety, semantic recall, or the new hierarchy behavior. PostgreSQL
session-principal forgery via arbitrary SQL and externally anchored audit
truncation remain explicit gaps. The public-text kit adds 2,088 probes over 87 National Archives paragraphs
with fictional ACLs: zero visibility and isolation failures, and 197 of 197
required exact-content hits. The independent example adapter passed too, but no unfamiliar
engineer has integrated it unassisted. The measured scale result still exposes a
reconciliation bottleneck.”

Close with: “The part I'd like you to inspect is the evidence chain: a precise
boundary, a test that initially missed bugs, faults that now make it fail, and
commands you can rerun.”

## Rehearsal checklist

- Time one complete run; record actual duration and trim repetition to five minutes.
- Explain **one of six**, **eight of eight**, and **1,471 of 1,486** without mixing
  their different denominators or purposes.
- Keep the pre-hierarchy live-demo distinction and synthetic-data caveat audible.
- Keep benchmark HTTP-search timings separate from full application reconciliation.
  Explain that external text has fictional permissions and exact-text queries.
  Leave limits beside every performance or safety number.
- Do not describe the rehearsal, article publication, external-user run, PR merge,
  or deployment of the new hierarchy as complete until each actually occurs.
