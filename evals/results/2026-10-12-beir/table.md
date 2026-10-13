# BEIR retrieval vs published numbers, 2026-10-12

nDCG@10 (mean [paired-bootstrap 95% CI over test queries]). Published BM25: BEIR paper Table 2; published bge-small: MTEB values in the BAAI/bge-small-en-v1.5 model card.

| dataset | queries | system | nDCG@10 | published | delta | Recall@100 | published |
|---|---|---|---|---|---|---|---|
| scifact | 300 | ref-app BM25 (no ACL filter) | 0.664 [0.619, 0.709] | 0.665 | -0.001 | 0.889 [0.854, 0.923] | 0.908 |
| scifact | 300 | bge-small (MTEB query prefix) | 0.713 [0.669, 0.755] | 0.713 | -0.000 | 0.942 [0.915, 0.967] | 0.942 |
| scifact | 300 | bge-small (no prefix, as platform `st`) | 0.720 [0.677, 0.762] | - | - | 0.953 [0.927, 0.977] | - |
| nfcorpus | 323 | ref-app BM25 (no ACL filter) | 0.311 [0.277, 0.345] | 0.325 | -0.014 | 0.236 [0.207, 0.266] | 0.250 |
| nfcorpus | 323 | bge-small (MTEB query prefix) | 0.343 [0.309, 0.378] | 0.343 | +0.000 | 0.311 [0.281, 0.341] | 0.311 |
| nfcorpus | 323 | bge-small (no prefix, as platform `st`) | 0.338 [0.304, 0.372] | - | - | 0.306 [0.276, 0.336] | - |
| fiqa | 648 | ref-app BM25 (no ACL filter) | 0.232 [0.208, 0.255] | 0.236 | -0.004 | 0.509 [0.478, 0.540] | 0.539 |
| fiqa | 648 | bge-small (MTEB query prefix) | 0.403 [0.374, 0.431] | 0.403 | -0.001 | 0.696 [0.668, 0.724] | 0.696 |
| fiqa | 648 | bge-small (no prefix, as platform `st`) | 0.385 [0.356, 0.413] | - | - | 0.687 [0.658, 0.714] | - |

## Permission tax (reference-app BM25, k=4 SYNTHETIC tenants)

| dataset | nDCG@10 unfiltered (full qrels) | filtered, in-tenant qrels | paired delta | filtered, full qrels | leaks (queries / items) | Wilson 95% upper (per item) | isolation diffs |
|---|---|---|---|---|---|---|---|
| scifact | 0.664 [0.619, 0.709] | 0.758 [0.715, 0.799] | 0.093 [0.072, 0.116] | 0.741 [0.698, 0.783] | 0/300 / 0/29996 | 1.28e-04 | 0/100 |
| nfcorpus | 0.311 [0.277, 0.345] | 0.280 [0.247, 0.314] | -0.031 [-0.051, -0.011] | 0.225 [0.196, 0.254] | 0/323 / 0/18806 | 2.04e-04 | 0/100 |
| fiqa | 0.232 [0.208, 0.255] | 0.315 [0.285, 0.343] | 0.083 [0.066, 0.100] | 0.254 [0.229, 0.279] | 0/648 / 0/64771 | 5.93e-05 | 0/100 |
