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
