# BEIR retrieval benchmark: data, licences, citations

Public-benchmark check of this repo's retrieval, compared against **published** numbers. This
directory holds the pinned data fetch; the harness and results are added separately.

## Datasets (test splits, official qrels)

| Dataset | Docs | Test queries | HF repo (pinned in `MANIFEST.json`) | Underlying licence / terms |
|---|---|---|---|---|
| SciFact | 5,183 | 300 | `BeIR/scifact`, `BeIR/scifact-qrels` | CC BY-NC 2.0 (BEIR paper App. E): research use allowed, non-commercial |
| NFCorpus | 3,633 | 323 | `BeIR/nfcorpus`, `BeIR/nfcorpus-qrels` | Not declared by the authors (BEIR paper App. E); NutritionFacts-derived research corpus |
| FiQA-2018 | 57,638 | 648 | `BeIR/fiqa`, `BeIR/fiqa-qrels` | Not declared by the authors (BEIR paper App. E); WWW'18 challenge Task 2 data |

The Hugging Face cards label the BEIR re-uploads `cc-by-sa-4.0`; that describes the re-host, not the
underlying sources. None of the three forbids research use, so none was dropped. Data is used for
evaluation only and **never committed or redistributed**: `data/` is gitignored.

## Fetch

```bash
python3 evals/beir/fetch.py      # stdlib only; sha256 + size verified against MANIFEST.json
```

`MANIFEST.json` pins each file to a Hugging Face commit, byte size and sha256. Corpus/queries
sha256 equal the LFS oids Hugging Face publishes; the small qrels `.tsv` files are not LFS, so
their sha256 was recorded when the manifest was created. A mismatch deletes the file and aborts.

## Citations

- Thakur, Reimers, Ruckle, Srivastava, Gurevych. *BEIR: A Heterogeneous Benchmark for Zero-shot
  Evaluation of Information Retrieval Models.* NeurIPS Datasets & Benchmarks 2021, arXiv:2104.08663.
  BM25 nDCG@10: Table 2; BM25 Recall@100: Table 9.
- Xiao, Liu, Zhang, Muennighoff. *C-Pack: Packaged Resources To Advance General Chinese Embedding*
  (BGE), arXiv:2309.07597. Model `BAAI/bge-small-en-v1.5` (MIT).
- Muennighoff et al. *MTEB: Massive Text Embedding Benchmark*, arXiv:2210.07316. Per-task values
  are the `model-index` metadata of the `BAAI/bge-small-en-v1.5` model card.
- SciFact: Wadden et al., *Fact or Fiction*, EMNLP 2020. NFCorpus: Boteva et al., ECIR 2016.
  FiQA: Maia et al., *WWW'18 Open Challenge: Financial Opinion Mining and Question Answering*.

## Run

```bash
uv venv -p 3.12 evals/beir/.venv                       # gitignored scratch venv
uv pip install -p evals/beir/.venv/bin/python -r evals/beir/requirements.txt
python3 evals/beir/fetch.py
evals/beir/.venv/bin/python evals/beir/run.py          # writes evals/results/2026-10-12-beir/{report.json,table.md}
```

CPU only, no paid API. First run downloads `BAAI/bge-small-en-v1.5` via fastembed (ONNX, MIT) and
embeds the corpora (document vectors are cached under the gitignored `data/`).
`--limit-queries N` is a smoke test; never publish its output. Unit tests: `cd app && python -m pytest -q test_beir_metrics.py`.

## What is compared

- **Reference-app BM25**: `app/permission_rag.py` with every doc ACL `{"*"}` (no filtering), one chunk
  per doc (title + text). It is *not* Elasticsearch/Anserini BM25: it has no stemming or stopwords,
  uses `k1=1.5, b=0.75` (BEIR's Elasticsearch default is `k1=1.2`), and takes the stdlib
  `[a-z0-9]+` tokenizer. Differences from the published figure are reported, not tuned away.
- **bge-small-en-v1.5**: exact cosine over fastembed embeddings. Reported twice: with the model card's
  query instruction (what MTEB uses) and without (what `platform/app/embeddings.py` does today).
  The platform's local backend is sentence-transformers (`EMBED_BACKEND=st`); fastembed is the same
  model through ONNX, so small numeric differences are possible.
- **Permission tax**: docs are split into k=4 tenants by a seeded hash of the doc id. These ACLs are
  **synthetic**; real ACLs are correlated with content, so the tax here is an exercise of the filter,
  not a forecast. Each query is run as a user in the tenant holding most of its gold docs.
  Reported: nDCG@10 unfiltered (full qrels) vs filtered scored on the in-tenant qrels (gold docs the
  user can read), the filtered run against full qrels, leaks (results outside the tenant, with a
  Wilson upper bound) and an isolation check on a seeded query sample (filtered full-corpus results
  must equal a tenant-only corpus: ids, order and scores).
- nDCG@10 is trec_eval `ndcg_cut` (linear gain), CIs are 10,000-resample paired bootstrap over queries.
