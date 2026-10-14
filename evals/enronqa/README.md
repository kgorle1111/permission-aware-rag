# EnronQA eval: leaks, recall and citation validity on real mail

Real emails and real questions that we did not write. Results with 95% confidence intervals:
[table.md](../results/2026-10-10-enronqa/table.md), every number in
[report.json](../results/2026-10-10-enronqa/report.json).

## Source and licence (verified 2026-10-10)

- Benchmark: EnronQA, Ryan, Xu, Nivera, Campos, "EnronQA: Towards Personalized RAG over Private
  Documents", arXiv:2505.00263 (the paper is CC BY 4.0).
- Data: Hugging Face `MichaelR207/enron_qa_0922`, linked from the paper as where "all data [is] released".
  Pinned revision `c0b3a9190fd970e83cfbe7d399a08860e43e221e`; we use the `test` parquet (161,772,154 bytes).
- **The dataset card declares no licence** (the HF `license` field is empty; the paper's CC BY 4.0 covers the
  article, not the data). The underlying emails are the FERC 2003 public release of the Enron corpus (the 2015
  edition, with people removed on request). We use it for research evaluation only, download it to a gitignored
  directory, and never commit or redistribute any email or question text. Record: "dataset card declares no
  licence; maintainer accepted research use without redistribution on 2026-10-10". If a licence is attached
  upstream later, check it before reusing this eval for anything other than research.
- Schema (one row per email): `email` (headers + body), `path` (`<mailbox>/<folder>/<n>.`), `user` (mailbox
  owner, 150 values), `questions`, `gold_answers`, `alternate_answers`. Each question is generated from its
  own row's email, so the gold source email is known. There is no Message-ID field.

## Permission mapping (exact)

Maintainer decision (2026-10-10): permissions come from the email headers, not only the mailbox.

- Document = one email; id = `path`. Dedup key = SHA-256 of the body (text after the `=====` line); rows
  sharing a body are one document (first path in sort order) whose readers are the union over those rows.
- **Readers = mailbox owner(s) + every header participant that maps to one of the 150 mailbox users.**
  Participants are the `Sender` address plus every address in `Recipients`, parsed with the stdlib `email`
  package (folded headers, several addresses per item, display names, case; names with no `@` are ignored).
  EnronQA flattens To/Cc/Bcc into the single `Recipients` field, so Cc/Bcc cannot be told apart; To/Cc/Bcc headers
  are also honoured if a future release carries them. ACL entries are `user:<mailbox>`; no groups, no `*`.
- **Address to user map, from the data only:** a user's address is the dominant `Sender` address over the emails
  in that user's own sent folders (folder name contains `sent`), accepted with >= 3 sent emails and >= 50% share;
  an address claimed by two users is dropped for both. Coverage is in `report.json`
  (`acl_model.address_map`); the full address table is written to the gitignored `data/address_map.json` and only
  counts are published. Participants not in the map (outside people, or users with no usable sent folder)
  grant nothing, so readers are an under-approximation of who really received the mail.
- Participants outside the 24 sampled mailboxes still count as readers (principals with no mailbox indexed).
- The earlier single-owner model (owner only; no email body appears in two mailboxes in this release, so every
  document had exactly one reader) is kept as a comparison section in the results.

## Design

1. `build/sample`: seeded (`20261010`) sample of 24 mailboxes with >= 100 emails; every email in them is
   indexed. 600 questions are drawn at random from all questions of those mailboxes.
2. Leaks: each question is asked as (a) the owner, (b) up to 3 other allowed participants, (c) 3 sampled
   mailbox owners who are NOT readers; 600 exact-content probes (the 8-token window with the highest summed IDF
   of a random email) are asked as 3 non-readers each. A leak is any top-10 chunk whose email the querying user
   cannot read per the readers dict (a plain dict check, independent of `PermissionRAG.can_read`). Controls: the
   probe text must retrieve its own email for its owner; the planted `NoFilter` mutant (`app/mutants.py`) must
   trip the oracle; an off-by-one ACL mutant (each email also readable by the next email's readers) must leak on
   targeted probes; a recipients-ignored mutant must show up as lost recall for legitimate readers (it
   under-grants, so it cannot leak).
3. Recall: hit@1/4/10 = a chunk of the gold source email is in the top-k chunks, for the owner and for other
   allowed participants, against the same chunks with every ACL set to `*` (no-ACL baseline). Wilson CI, plus a
   mailbox-clustered bootstrap CI because questions within a mailbox are not independent.
4. Citation validity (paid step, `app/llm.py`, pinned `claude-haiku-4-5-20251001`): top-4 chunks go to
   `llm.ask`; `ask` cites **document ids** (not chunk ids). Checked per citation: points to a retrieved doc and the
   oracle says the owner can read it; supports the gold answer (deterministic: >= 0.6 of the gold answer's content
   tokens, minus question tokens, occur in the cited retrieved text; threshold fixed in advance; the rule is
   sanity-checked on the full gold email text); SQuAD-normalized exact match and token F1 against gold plus
   alternate answers. No LLM judge.
5. Spend: tokens from the API `usage` are priced at Haiku 4.5 list price; a call is not made if spend so far plus
   a worst-case estimate for it would pass `--max-usd` (default 5).

## Reproduce

```
pip install -r evals/enronqa/requirements.txt         # pyarrow, eval-only
python3 evals/enronqa/fetch.py                        # ~162 MB into data/ (gitignored); sha256-verified against MANIFEST.json
python3 evals/enronqa/run.py --no-llm                 # ~10 min, no API key needed; writes evals/results/2026-10-10-enronqa/
export ANTHROPIC_API_KEY=...                          # paid step only; read from the environment, never printed
python3 evals/enronqa/run.py --smoke 5                # prints cost per question, writes nothing
python3 evals/enronqa/run.py --llm-n 600 --max-usd 5  # full run
cd app && python3 -m pytest -q test_enronqa_eval.py   # metric/ACL code on a tiny synthetic fixture (scaffolding, not evidence)
```

## Caveats

- Questions were LLM-generated from each email, so they share vocabulary with it; BM25 recall here is likely
  higher than for questions people write unprompted.
- Leak counts cover returned chunks. Score side channels are covered by `app/run_evals.py` (isolation gate), not here.
- `llm.ask` uses the repo's underwriting system prompt, unchanged; it is not tuned for email QA.
- 24 mailboxes out of 150 and the test split only; the exact-content probes use one shingle per email.

## Platform retrieval rungs on this data (2026-10-12)

Does semantic retrieval help on real mail, without leaking? The platform's local-model rungs (hash baseline,
fastembed dense, SPLADE hybrid, cross-encoder rerank) run through the platform's own ingest and `retrieve()` on
an EnronQA subsample with the header-derived reader ACLs above. Table with CIs:
[table.md](../results/2026-10-12-rungs-on-enronqa/table.md); every number in
[report.json](../results/2026-10-12-rungs-on-enronqa/report.json). Harness: `platform/scripts/rung_enronqa.py`
(reuses this directory's `run.py` for sampling, ACLs, Wilson and bootstrap code).

**Subsample, and why.** 12 mailboxes with 100-400 emails each (seed 20261010): 2,678 emails, 22,952 platform
chunks, 300 questions whose gold email is in the index, 3 non-reader users per question, 60 exact-content probes.
The platform chunks per paragraph, so the 24-mailbox sample above would be 137,684 chunks there. Measured: bge-small
6.6 ms/chunk, SPLADE 27 ms/chunk (62 min for SPLADE alone), and embedded Qdrant scans every chunk per query (150 ms
at 21k chunks for dense, 0.7-1.0 s for hybrid). Full size would miss the 60 min/rung budget; this size took 4-90 min
per rung (about 3.2 h in all). Capping mailbox size keeps 12 independent clusters for the bootstrap.

```
python3 -m venv .rungvenv && .rungvenv/bin/pip install -r platform/requirements-dev.txt -r platform/requirements-models.txt pyarrow
python3 evals/enronqa/fetch.py                       # as above, if data/ is empty
cd platform
../.rungvenv/bin/python scripts/rung_enronqa.py --rungs baseline,dense,dense_floor_0.1,hybrid,hybrid_rerank,hybrid_rerank_no_floor \
    --scratch /tmp/enron_rungs --output-dir ../evals/results/2026-10-12-rungs-on-enronqa     # no API key needed
# the committed report also merged two cheaper re-runs on the same sample and scratch dir:
../.rungvenv/bin/python scripts/rung_enronqa.py --phase invariance --rungs <same list> --scratch /tmp/enron_rungs --output-dir <same>
../.rungvenv/bin/python scripts/rung_enronqa.py --phase ranks      --rungs <same list> --scratch /tmp/enron_rungs --output-dir <same>
```

**What it shows.**

- Dense beats the hash baseline on this data: hit@1 +12.7 points (paired, mailbox-clustered CI +6.8 to +17.6),
  hit@10 +10.0 (+4.2 to +16.5). The platform's default similarity floor (0.55) cost nothing here: it returned
  nothing for 7 of 900 non-reader questions and for none of the owner queries, so `dense` and `dense_floor_0.1`
  are identical on recall.
- Hybrid over dense is small: hit@1 +2.3 (-0.7 to +5.8), hit@10 +2.0 (0.0 to +4.5). Rerank adds hit@1 over dense
  (+7.3, +4.0 to +10.7) but nothing at hit@10 (+1.3, -1.2 to +3.5): it reorders the top, it does not find more.
  Between adjacent rungs (hybrid vs rerank) the paired intervals include 0.
- No leaks on any rung: 0/1,080 non-reader attempts (900 questions, 180 exact-content probes), Wilson 95% upper
  bound 0.35%. The positive control works: with the ACL filter disabled (the NoFilter bug) the same oracle flags
  100/100 non-reader queries on every rung, so a 0 elsewhere is a measurement, not a blind oracle.
- Hidden-document invariance holds for the dense rungs (0/75 differing, ignoring order among exact-score ties).
  It does NOT hold, as measured, for `hybrid` (34/75), `hybrid_rerank` (1/75) and `hybrid_rerank_no_floor` (3/75).
  The control says why: re-ingesting the *same* corpus, nothing hidden, in reverse order changes hybrid on 14/25
  queries and rerank on 1/25. Mail is full of identical quoted paragraphs, which tie exactly; tie order follows ingest
  order (chunk ids; not traced inside Qdrant), and RRF turns a tie order into different fused scores. So this is ingest-order dependence, not a
  measured dependence on hidden documents, but it is a real property of hybrid ranking and it shows up as a
  score-identity failure. The synthetic-corpus table (no duplicate text) could not show it. Not diagnosed further.

**What this does and does not show.** The questions were written by a model from each email, so they share
vocabulary with it; this favours lexical matching, and the hash baseline's 75% hit@10 is probably high for
human-written questions. Gold is one email, but many emails quote one another, so a hit on a near-duplicate
counts as a miss (recall is understated, for every rung alike). It is one sample of 12 mailboxes (17% of the
24-mailbox sample's chunks), the test split only, one seed, and 300 questions: single-rung CIs are about 11 points
wide. Readers are header-derived and an under-approximation of who really received a mail. Rerank runs on the
platform's default floor of -10, calibrated on synthetic data; `no_floor` is identical on recall. Latency is one
run on a shared laptop (the last rung took 90 min against 33 for the one before it, for the same work) and includes
the audit write and the denied-count search; read it as an order of magnitude, and note embedded Qdrant is not
what production would use. Leak checks cover returned chunks, not score side channels. Nothing here evaluates
answers or citations (no LLM was called).
