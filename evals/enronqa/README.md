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
  directory, and never commit or redistribute any email or question text. If a licence is attached upstream
  later, check it before reusing this eval for anything other than research.
- Schema (one row per email): `email` (headers + body), `path` (`<mailbox>/<folder>/<n>.`), `user` (mailbox
  owner, 150 values), `questions`, `gold_answers`, `alternate_answers`. Each question is generated from its
  own row's email, so the gold source email is known. There is no Message-ID field.

## Permission mapping (exact)

- Document = one email. Its id is `path`. ACL = `["user:<mailbox>"]` where `<mailbox>` is the `user` field.
- Dedup key = SHA-256 of the email body (the text after the `=====` header). Rows that share a body are one
  document whose id is the first path in sort order and whose ACL is the **union** of the mailboxes it was found in.
- No synthetic groups, no `*` entries.
- **Finding:** in this release no body occurs twice (0 multi-owner documents among the 14,542 sampled; 0 body
  collisions in all 73,772 test emails), so the union rule is implemented and unit-tested but never exercised
  by the real data. Every document has exactly one reader. A non-owner therefore sees an empty visible set, which
  makes the leak check a test of the filter, not of subtle sharing.

## Design

1. `build/sample`: seeded (`20261010`) sample of 24 mailboxes with >= 100 emails; every email in them is
   indexed. 600 questions are drawn at random from all questions of those mailboxes.
2. Leaks: each question is asked as the owner and as 3 random non-owners; 600 exact-content probes (the
   8-token window with the highest summed IDF of a random email) are asked as 3 non-owners each. A leak is any
   top-10 chunk whose email the querying user cannot read per `owners_by_doc` (a plain dict check, independent of
   `PermissionRAG.can_read`). Controls: the probe text must retrieve its own email for its owner, and the planted
   `NoFilter` mutant from `app/mutants.py` must trip the same oracle.
3. Recall: hit@1/4/10 = a chunk of the gold source email is in the top-k chunks, querying as the owner, against
   the same chunks with every ACL set to `*` (no-ACL baseline). Wilson CI, plus a mailbox-clustered bootstrap CI
   because questions within a mailbox are not independent.
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
