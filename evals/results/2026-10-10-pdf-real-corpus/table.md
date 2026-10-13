# Real-PDF corpus: ingestion, OCR accuracy, table coverage, leak check (2026-10-10)

## Corpus (all US federal publications, 17 USC 105; fetched by `evals/pdf_corpus/fetch.py`)

| publisher | kind | files | bytes |
|---|---|---|---|
| CFPB | born_digital | 2 | 3,193,894 |
| FDIC | born_digital | 30 | 15,480,855 |
| FEMA | born_digital | 1 | 192,188 |
| Federal Register | scanned | 24 | 243,705,380 |
| GAO | born_digital | 7 | 20,378,494 |
| HUD | born_digital | 1 | 7,022,355 |
| IRS | born_digital | 29 | 51,140,926 |
| Treasury | born_digital | 2 | 6,756,712 |
| **total** | | **96** | **347,870,804** |

Ingested with `platform/app/pdf_ingest.py` (first 150 pages of each file): 96 documents, 4,073 pages, 44,060 paragraph units, 5 pages through OCR fallback (pages with no usable text layer), 8,318 running head/foot lines removed.

## OCR accuracy vs the page's own text layer (born-digital pages)

Ground truth = the characters of each page's text layer (column-aware reading order from `pdf_ingest`). Pages are rasterized by pdfium at the stated DPI and OCR'd by Tesseract through the production `ocr_page`. CER/WER = pooled Levenshtein edits / truth length, 95% percentile bootstrap over pages (2,000 resamples, seed 0). 'word recall, order-free' counts truth words found anywhere in the OCR output, so it ignores reading-order differences. Pages were drawn at random (seed 20261010) from pages with >= 300 characters of text.

### By DPI

| group | pages | CER (95% bootstrap CI) | WER (95% bootstrap CI) | word recall, order-free | pages with CER <= 5% (Wilson 95%) |
|---|---|---|---|---|---|
| 200 DPI (all publishers) | 146 | 7.27% [4.95%, 10.04%] | 9.33% [6.78%, 12.18%] | 95.41% | 107/146 = 73.29% [65.58%, 79.80%] |
| 300 DPI (all publishers) | 146 | 7.11% [4.83%, 9.79%] | 9.15% [6.72%, 11.97%] | 96.38% | 107/146 = 73.29% [65.58%, 79.80%] |

### By publisher

| group | pages | CER (95% bootstrap CI) | WER (95% bootstrap CI) | word recall, order-free | pages with CER <= 5% (Wilson 95%) |
|---|---|---|---|---|---|
| CFPB @300 | 15 | 14.32% [6.34%, 22.81%] | 15.35% [6.82%, 24.99%] | 97.19% | 7/15 = 46.67% [24.81%, 69.88%] |
| FDIC @300 | 40 | 5.36% [2.78%, 10.06%] | 5.46% [3.76%, 8.19%] | 98.06% | 35/40 = 87.50% [73.89%, 94.54%] |
| FEMA @300 | 1 | 18.84% [18.84%, 18.84%] | 32.26% [32.26%, 32.26%] | 92.47% | 0/1 = 0.00% [0.00%, 79.35%] |
| GAO @300 | 20 | 9.82% [4.93%, 15.93%] | 18.57% [8.01%, 31.82%] | 94.96% | 11/20 = 55.00% [34.21%, 74.18%] |
| HUD @300 | 15 | 0.93% [0.61%, 1.35%] | 2.94% [2.23%, 3.82%] | 97.78% | 15/15 = 100.00% [79.61%, 100.00%] |
| IRS @300 | 40 | 5.41% [2.57%, 9.74%] | 10.16% [5.34%, 16.31%] | 95.09% | 29/40 = 72.50% [57.17%, 83.89%] |
| Treasury @300 | 15 | 16.65% [4.66%, 31.55%] | 8.25% [4.27%, 14.27%] | 96.56% | 10/15 = 66.67% [41.71%, 84.82%] |
| CFPB @200 | 15 | 14.06% [5.90%, 23.07%] | 14.70% [6.32%, 25.12%] | 96.85% | 7/15 = 46.67% [24.81%, 69.88%] |
| FDIC @200 | 40 | 4.75% [2.68%, 8.58%] | 4.83% [3.55%, 6.79%] | 97.85% | 35/40 = 87.50% [73.89%, 94.54%] |
| FEMA @200 | 1 | 19.63% [19.63%, 19.63%] | 33.33% [33.33%, 33.33%] | 92.47% | 0/1 = 0.00% [0.00%, 79.35%] |
| GAO @200 | 20 | 8.87% [4.23%, 14.92%] | 17.49% [6.83%, 30.74%] | 95.07% | 12/20 = 60.00% [38.66%, 78.12%] |
| HUD @200 | 15 | 0.74% [0.56%, 1.01%] | 2.94% [2.44%, 3.57%] | 97.64% | 15/15 = 100.00% [79.61%, 100.00%] |
| IRS @200 | 40 | 6.53% [3.17%, 11.20%] | 11.53% [6.46%, 18.14%] | 92.86% | 29/40 = 72.50% [57.17%, 83.89%] |
| Treasury @200 | 15 | 17.11% [4.70%, 32.47%] | 7.62% [4.00%, 13.18%] | 96.76% | 9/15 = 60.00% [35.75%, 80.18%] |

Worst five pages at 300 DPI (CER, publisher, file, page index): 0.76 IRS cdc04aced5-p502.pdf p25; 0.68 FDIC 12ccf1a-section2-1.pdf p0; 0.67 Treasury OC2025AnnualReport.pdf p76; 0.67 FDIC 51d171-section16-1.pdf p68; 0.62 Treasury nnual_Report_Final.pdf p1

## Federal Register scans vs GPO's embedded OCR layer (NOISY BASELINE, NOT GROUND TRUTH)

GPO's text layer is itself uncorrected machine OCR of the same scan. Numbers below measure how closely our Tesseract output *agrees with another OCR engine*; disagreement can be GPO's error or ours, and the metric cannot tell which. No accuracy claim is made for scans.

| group | pages | CER (95% bootstrap CI) | WER (95% bootstrap CI) | word recall, order-free | pages with CER <= 5% (Wilson 95%) |
|---|---|---|---|---|---|
| Federal Register, 300 DPI | 32 | 75.16% [74.40%, 75.86%] | 90.76% [89.70%, 91.80%] | 79.56% | 0/32 = 0.00% [0.00%, 10.72%] |

32 pages from 19 issues (1936-1993).

## Table extraction: coverage only (no accuracy claim)

FinTabNet (the labelled table benchmark we looked at) is licensed CDLA-Permissive according to its dataset cards (Hugging Face `bsmock/FinTabNet.c` states `cdla-permissive-2.0` and relays the original CDLA-Permissive; the official IBM Data Asset Exchange page is deprecated and the `dax-cdn.cdn.appdomain.cloud` download host no longer resolves, so the original LICENSE.txt could not be read). The usable PDFs are not obtainable from an official source (FinTabNet.c ships annotations only and its extract script needs the dead IBM archive), so pdfplumber table-structure accuracy was **not measured**. No table labels were invented. What follows is coverage on the federal corpus: pdfplumber's default ruled-line strategy, tables with >= 2 rows and >= 2 columns.

| publisher | docs | docs with >= 1 table | tables | median rows | median cols | max rows x cols |
|---|---|---|---|---|---|---|
| CFPB | 2 | 1 | 39 | 4 | 6 | 16x10 |
| FDIC | 30 | 7 | 39 | 5 | 4 | 34x10 |
| FEMA | 1 | 0 | 0 | - | - | - |
| Federal Register | 24 | 0 | 0 | - | - | - |
| GAO | 7 | 3 | 16 | 4.5 | 4.5 | 4x15 |
| HUD | 1 | 1 | 13 | 5 | 4 | 8x9 |
| IRS | 29 | 25 | 434 | 9.0 | 5.0 | 39x18 |
| Treasury | 2 | 1 | 17 | 3 | 6 | 4x6 |
| **all** | 96 | 38 | 558 | 7.0 | 5.0 | |

Unruled (whitespace-aligned) tables are not detected by the default strategy, so these counts are a floor, not a census.

## Leak check on the real text (ACLs are SYNTHETIC: one group per publisher)

96 documents, 44,060 chunks ingested through the platform (`ingest_corpus`). Text is real; who may read what is invented for the test (FDIC -> `group:examiners`, IRS -> `group:tax`, CFPB -> `group:compliance`, HUD -> `group:housing`, GAO and Treasury -> `group:policy`, FEMA -> `group:flood`, Federal Register -> `group:archive`; plus an outsider with no group).

| check | result |
|---|---|
| exact-content probes (12-word shingles unique to one document) | 384 probes |
| forbidden attempts (probe x principal who may not read the document) | 2,688 |
| **leaks** (a result from a forbidden document) | **0** (Wilson 95% upper bound 0.14%) |
| authorized control (owning group finds its own document) | 365/384 = 95.05% |
| isolation diff for `group:tax` (results with vs without every unreadable document) | 0 differing of 150 queries (1 before canonicalizing the order of equal-score results) |
| isolation diff for `group:examiners` (results with vs without every unreadable document) | 0 differing of 150 queries (0 before canonicalizing the order of equal-score results) |

## Caveats

- Born-digital rasterization is clean, noise-free input: OCR numbers are an upper bound for real scans, which add skew, speckle and fading. They say nothing about handwriting or photographs.
- Text-layer 'ground truth' is exact for characters but not for reading order; the order-free word recall column is the order-insensitive view.
- Federal Register agreement is a noisy baseline, as labelled; it is not accuracy.
- The leak check is real text with invented permissions. It exercises ACL enforcement on realistic chunk sizes and vocabulary; it does not validate any real organisation's permission model.
- Document-sourced text is data: nothing extracted from these PDFs was executed or treated as an instruction.
- Isolation: one `group:tax` query returned the same four results in a different order when unreadable documents were removed. The tied scores were ordered by internal chunk id, which depends on corpus composition, not on document text. Content, scores and document ids were identical (`iso_debug.py` shows the case). It is reported rather than hidden, and not treated as a content leak.
- Authorized control is 95%, not 100%: 19 of 384 exact 12-word probes did not return their own document in the top 4 (cause not traced; likely repeated boilerplate across publications and a lexical hash embedder). That is retrieval quality, unrelated to the zero-leak count.
- FinTabNet table accuracy was not measured (see above).
