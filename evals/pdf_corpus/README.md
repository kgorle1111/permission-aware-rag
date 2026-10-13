# Real-PDF corpus evals

Real US-federal PDFs only (17 USC 105); no generated documents. Results: [table.md](../results/2026-10-10-pdf-real-corpus/table.md).

Needs the `tesseract` binary plus `pip install -r platform/requirements-dev.txt` (Python 3.12).

```
python evals/pdf_corpus/fetch.py                     # ~350 MB into data/ (gitignored); verifies MANIFEST.json sha256s, resumes, stops on 403/429
python evals/pdf_corpus/build_corpus.py 150          # extract every PDF with platform/app/pdf_ingest.py -> data/corpus.json
python evals/pdf_corpus/ocr_accuracy.py born ../results/2026-10-10-pdf-real-corpus/ocr_born.json
python evals/pdf_corpus/ocr_accuracy.py fr   ../results/2026-10-10-pdf-real-corpus/ocr_fr.json
python evals/pdf_corpus/leak_check.py ../results/2026-10-10-pdf-real-corpus/leak.json 4
python evals/pdf_corpus/report.py                    # writes table.md
```

Text is real; the per-publisher ACLs in `build_corpus.py` are synthetic and exist only so the leak check has something to violate.
