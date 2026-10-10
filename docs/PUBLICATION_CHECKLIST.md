# Publication and rehearsal preparation

**Status: private draft; do not publish.** The article has not been published outside this
repository; no timed human rehearsal, recording, or unassisted external
integration is claimed. This checklist prepares those actions without posting,
sending invitations, or changing the live demo.

## Article metadata and final review

| Field | Prepared value |
|---|---|
| Title | A permission-aware retriever is only as convincing as its leak tests |
| Subtitle | A leak gate missed five of six planted faults; isolation checks and measured revocation exposed what to fix next. |
| Private source | `marketing/posts/permission-aware-rag-article.md` (gitignored; local only) |
| Audience | Hiring managers, AI engineering interviewers, engineers evaluating their own retrievers |
| Summary | Permission filtering must protect visible scores as well as returned IDs. This reference project publishes test misses, hierarchy checks, durable revocation recovery, and a reproducible adapter kit with explicit limits. |
| Suggested tags | RAG, retrieval, access control, security testing, reproducible engineering |
| Publication venue, author/byline, date | Publication paused by maintainer; obtain a new explicit publishing instruction before posting |
| Canonical article URL | Pending publication; record the actual public URL after verification |
| Evidence revision | Record exact published Git commit; current work is stacked in open PRs, not merged |

Before publication:

- Verify links resolve from the publishing venue; convert relative repository
  links to the chosen immutable Git revision. Recheck PR status and live endpoint.
- Keep the original negative results: label gate 1/6, combined faults 8/8,
  baseline folder 104.92 s missing 60 s, and optimized folder 13.31 s on the
  measured synthetic corpus. Distinguish retrieval faults from 1,646/1,661
  mutation kills with 15 reviewed equivalents.
- Retain the scope of 0/1,350 frozen flat-ACL probes, 0/124 hierarchy differences,
  and public text with fictional permissions. The live demo is a stable
  pre-hierarchy image; it does not demonstrate the current journal implementation.
- Keep query timings separate from complete API/generation latency, and streamed
  harness ingestion separate from production ingestion. Do not claim query
  speedup, concurrency safety from the benchmark, or measured quantization savings.
- Proofread the rendered article on desktop and mobile. Verify code fences,
  tables, evidence links, attribution and source rights; inspect screenshots/logs
  for credentials or private data. Record any substantive edits with their source.
- After an authorized actual posting, verify an unauthenticated reader can open
  the article and evidence links. Record URL, date and revision. A local draft or
  an inaccessible preview is not a published result.

## Five-minute human rehearsal rubric

Use the [walkthrough](WALKTHROUGH.md). Preload evidence and terminal outputs;
the script's timestamps are targets, not an observed rehearsal duration. A
speaker should time a complete delivery and record the actual checkpoints.

| Target interval | Observable requirement |
|---|---|
| 0:00–0:40 | State the retrieval isolation claim and synthetic/pre-hierarchy demo scope. |
| 0:40–1:25 | Show two caller roles and permitted sources; explain filtering before ranking. |
| 1:25–2:20 | Explain why 1/6 was inadequate and why the combined gate catches 8/8. |
| 2:20–3:10 | Explain AND across ACL levels with one caller satisfying different levels. |
| 3:10–4:05 | Show fail-closed recovery, mutation evidence, baseline miss and scoped optimization. |
| 4:05–5:00 | Bound zero-leak results, show the kit, and state external integration remains pending. |

The 55-second reliability/scale segment is dense: show the linked table and
choose one failure/recovery example rather than reciting every statistic. Trim
repetition and use the evidence table for follow-up questions. Do not omit the
negative result or its measurement limits to meet the timer.

Record: speaker/date, tested revision, total elapsed time, each actual checkpoint,
listener's explanation of the claim, confusing terms, demo failure/fallback,
and edits needed. Success requires a complete delivery in at most five minutes,
audible limits, correct denominators, working evidence navigation, and a listener
who can distinguish the claim from production certification. If over time or
misunderstood, revise and rerun; record both attempts. A recording is optional
and requires the speaker's choice; no recording has been made here.

After each rehearsal, ask the listener to explain the score side channel,
hierarchical ACL example, interrupted-revocation behavior and main evidence gaps.
Record their actual answers instead of treating script completion as comprehension.
