# A permission-aware retriever is only as convincing as its leak tests

A retrieval system can return only permitted documents and still reveal something
about a forbidden one. Hidden documents can change the scores and ordering of the
results a caller can see. That made the most useful result in this project a
negative one: the original hand-labeled leak gate caught only **one of six**
deliberately faulty retrievers.

The project is an engineering portfolio: a small underwriting workbench, a
readable reference retriever, and a service implementation. Its contribution is
inspectable permission behavior and reproducible evidence. The insurance domain
makes the restrictions concrete; the documents and users are synthetic.

## What the first gate missed

A conventional negative assertion asks whether a forbidden document appears in
the top results. It misses a retriever that ranks the entire corpus before
removing forbidden hits, or calculates BM25 statistics using hidden documents.
Those implementations can change visible results without returning a forbidden
ID. More hand labels would not directly test that property.

The added isolation oracle compares the same caller and query against two
corpora: the full corpus, and a corpus containing only what that caller can read.
It compares both chunk IDs and scores. Hidden content should not change either
for the reference BM25 retriever. The original isolation gate caught all six
planted faults; the combined gate now catches **eight of eight**, including two
faults in hierarchical permissions. The older frozen document-ID leak evaluation
caught four of the original six. These are complementary checks, not
interchangeable scores. [Original results](../evals/results/2026-10-03-v2/table.md)

## Permissions must survive the chunk boundary

A document grant can contain a narrower section grant and a still narrower
paragraph grant. A caller must satisfy every level. Someone in HR may satisfy a
document grant while their own user ID satisfies its section grant; they should
not need the same principal string to appear at both levels.

Flattening the levels into an intersection wrongly denies that caller. Combining
them with OR can reveal a restricted paragraph. The implementation keeps three
independent ACL levels and requires a match at each before ranking. The
reference Python retriever, PostgreSQL row-level security policy, and Qdrant
filter express that same rule. Chunks preserve paragraph policy boundaries.

The current isolation suite has **124 comparisons**, including eight explicit
hierarchy probes whose expected readable chunk IDs are authored independently of
the tested ACL matcher. There were zero differences. This checks deterministic
examples; it is not a statistical safety claim for arbitrary hierarchies.
[Implementation and measured results](../evals/results/2026-10-07-hierarchy/table.md)

## Revocation has to work when a write fails

The service stores an authoritative permission mirror in SQL and grants in
Qdrant payloads. Updating both without a barrier would expose partially applied
state. Permission changes therefore persist a pending state before remote writes;
queries remain denied until reconciliation succeeds. Interrupted deletion
requires full reingestion. Legacy indexes missing paragraph policy also require
reingestion rather than guessing a broader grant.

For scale preparation, documents with the same grants share filtered payload
updates, while section and paragraph grants remain unchanged. Four keyword
indexes, on-disk original vectors and payloads, and int8 quantization are
configured for remote Qdrant. A real Qdrant 1.18.0 diagnostic verified the
configuration, hierarchy, bulk revocation and child-policy preservation. That
establishes functional behavior. It does not establish memory savings or ANN
recall. The subsequent scale run measured a different limitation: the remote
search path was fast while end-to-end permission reconciliation was slow.

## The scale run exposed a revocation bottleneck

A streamed benchmark loaded **100,000 documents and 1.5 million chunks** into a
real Qdrant 1.18.0 server accessed over HTTP, with 4 CPU cores and a 5,500 MiB
memory limit. The harness populated a SQLite permission mirror; it did not use
the production `ingest_corpus` path. Filtered-search p95 latency over 75 samples
per visibility bucket was **4.15 ms at 1%, 4.45 ms at 10%, and 8.00 ms at 60%**.
Those timings measure the remote filtered search, not the complete authenticated
API, generation, or an isolation proof under ANN ranking.

The actual one-document permission update through `sync_once` took **102.29
seconds**. Grouped vector writes did not make application revocation a single
round trip: reconciliation still scanned the full SQL mirror and replayed all
document grants. The 10,000-document folder update took **104.92 seconds**, missing its
60-second target; 9,999 grants changed because the first document was already
revoked. Both checks found zero remaining granted points. This is a negative
result worth retaining: a fast engine query does not imply a fast permission-change
workflow. These measurements also do not
establish production ingestion throughput, compressed resident-memory savings,
or ANN recall. [Scale method and results](../evals/results/2026-10-08-scale/table.md) ·
[Raw report](../evals/results/2026-10-08-scale/report.json)

## Test the tests, then keep the limits beside the numbers

The service's full mutation campaign generated **1,486 mutants**. Tests killed
**1,471**; the remaining **15** were individually reviewed as equivalent and
pinned to source and mutant hashes. They remain survivors in the reported score.
Eight regression tests killed 27 meaningful survivors, covering migration
reflection, remote index arguments, batching, input ambiguity and retry behavior.
No unchecked mutants, timeouts or suspicious outcomes remained when the strict
validator passed.

The frozen synthetic evaluation uses 210 documents and the unchanged hash
`db6b5dcb51a5`. Both reference backends produced **0/1,350 forbidden-document
leaks**. Recall@4 was 1,170/1,170 in memory and 1,166/1,170 with pgvector. The
95% leak-rate upper bound is 0.28% for this template-derived distribution.
Correlated probes, flat ACLs and exact-wording queries prevent extending that
bound to real documents, semantic recall, or the new hierarchy behavior.

At the hierarchy checkpoint, local verification included 58 reference tests with
real PostgreSQL and 322 service tests at 98.91% combined branch-aware coverage.
The subsequent benchmark and adapter batch brought the full reference run to
80 passing tests with real PostgreSQL. Coverage is a useful
floor, not proof of security. On review of
[PR #6](https://github.com/kgorle1111/permission-aware-rag/pull/6) at revision
`6ea35f04c32c2e802b2d3fbce497fee852f12514`, reference, pgvector, both Python
coverage jobs, container smoke and mutation CI had all succeeded. The PR was
open; passing checks do not mean it was merged.

## Try the evidence

Use the PR branch to reproduce this dated evidence; the default branch may not
contain an open PR:

```bash
git clone https://github.com/kgorle1111/permission-aware-rag
cd permission-aware-rag
git checkout codex/tier1-hierarchy
cd app
python3 run_evals.py
python3 run_evals.py --mutants
python3 eval_scale.py
```

The first command reports 14/14 labeled recall, zero leaks and zero isolation
differences out of 124. The second must catch 8/8 planted faults. The third
checks the frozen synthetic evaluation. PostgreSQL and service runs require
their dependencies and disposable infrastructure; use the
[results reproduction instructions](../evals/results/2026-10-07-hierarchy/table.md)
and [agent command reference](../AGENTS.md#commands).

The [live AWS workbench](https://permission-rag-demo.b9hphyfz7skjm.us-east-2.cs.amazonlightsail.com/)
lets a reviewer change roles and inspect sources. It runs a stable
**pre-hierarchy** reference image, with synthetic data and retrieval-only answers.
Demo role selection is not real-user authentication; audit history is ephemeral
on container replacement. It demonstrates the interface, not the new hierarchy
or scale results. [Hosting details](../deploy/aws/README.md)

## External text and a small adapter contract

A second evaluation uses **87 externally authored paragraphs** from the National
Archives Constitution transcription, with explicitly **fictional** document,
section and paragraph ACLs. Eight principal scopes and three top-k values produce
2,088 probes. Both the reference BM25 adapter and an independent term-overlap
example had **0/2,088 visibility failures, 0/2,088 isolation differences, and
197/197 required exact-content hits**. A controlled warmed revocation passed.
The kit rejected all eight planted faults on both its synthetic controls and
public-text fixture. Positive controls catch false denials, so an always-empty
retriever cannot pass simply by disclosing nothing.

This is external text I did not author, but its permissions are invented and its
questions copy paragraph text. It is not recovered private legal input, genuine
access-policy evaluation, held-out underwriting relevance, or semantic recall.
The eight original faults include mixed hierarchy effects; a separate flat-ACL
IDF control exposes score/ranking differences while returning only authorized IDs.
[Public-input results and provenance](../evals/results/2026-10-08-public-kit/table.md)

The [kit](../evals/kit/README.md) defines a small canonical-chunk adapter interface
and an independently computed visibility oracle. The example adapter demonstrates
a second implementation, not an unfamiliar engineer's unassisted integration.
A Node promptfoo shim was directly exercised; the full promptfoo CLI/SDK remains
unexecuted. No third-party competitor package or model was run.

## Remaining evidence and known gaps

The scale and external-text artifacts above are measured, while fixing the
reconciliation bottleneck, full runner integration and an unassisted external
adapter run remain open. The missing historical
296-chunk legal corpus has not been recovered or evaluated. An unassisted run by
an external engineer is still a target, not a completed result.

PostgreSQL session-principal forgery through arbitrary SQL remains open; the
hierarchy policy does not fix it. The reference audit checkpoint detects JSONL
tail deletion, but rewriting both local files or truncating the pgvector audit
requires an independently retained anchor. Prompt boundaries and citation-ID
checks do not prove factual grounding or universal prompt-injection resistance.
[Threat model](THREAT_MODEL.md)

The engineering lesson is to make the claimed boundary executable, plant faults
that violate it, and publish what the tests miss. A zero-leak number becomes
useful when a reviewer can see its distribution, its failure cases, and the
commands that produced it.
