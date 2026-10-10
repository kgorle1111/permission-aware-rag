# Real PDF fixtures

Real US federal publications (17 USC 105: no copyright in works of the US government). No generated or synthetic documents are used anywhere in the PDF tests; the OCR test rasterizes a page of these files at test time.

| file | source URL | sha256 |
|---|---|---|
| fdic-section3-1.pdf | https://www.fdic.gov/resources/supervision-and-examinations/examination-policies-manual/section3-1.pdf | f22c5c7cd9df71686b95f0462dde71d2eb9f208ef5c029167776c93a8e9a61ad |
| fema-bulletin-w-25004.pdf | https://agents.floodsmart.gov/sites/default/files/bulletins/W-25004/w-25004.pdf | 1d8fd99db19429ac862614c346e52e8efd6f20284ef637ca5ae6792879213380 |
| fdic-section17-1.pdf | https://www.fdic.gov/resources/supervision-and-examinations/examination-policies-manual/section17-1.pdf | ceed222dd62efe0c864e4248185ab4b7768d5225a543eecbfa7df10863d02767 |
| fdic-section21-1.pdf | https://www.fdic.gov/resources/supervision-and-examinations/examination-policies-manual/section21-1.pdf | 5f1a6f2bdab6d1719d69c4cfbfe2982d9f915f4a3bd2af5547faa5db54a59095 |
| irs-p542.pdf | https://www.irs.gov/pub/irs-pdf/p542.pdf | 9bae90126317b0de17313cdae8245209fd9de403a455d52f418f4a5b85c13b30 |

Fetched 2026-10-10 by `evals/pdf_corpus/fetch.py`. `test_pdf_ingest.py` re-checks the hashes.
