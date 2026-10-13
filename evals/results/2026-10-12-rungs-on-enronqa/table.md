# Platform retrieval rungs on EnronQA (2026-10-13)

12 mailboxes, 2678 emails, 22952 platform chunks, 300 machine-written questions, readers derived from email headers. Owner = the mailbox the question's email came from; a hit is any top-k chunk of the gold email.

## Recall (owner queries; Wilson 95% CI in brackets)

| rung | floors | hit@1 | hit@4 | hit@10 | MRR@10 [cluster-bootstrap CI] | returned nothing |
|---|---|---|---|---|---|---|
| baseline | dense 0.1 | 49.7% [44.0, 55.3] | 68.0% [62.5, 73.0] | 75.0% [69.8, 79.6] | 0.581 [0.507, 0.665] | 0/300 |
| dense | dense 0.55 | 62.3% [56.7, 67.6] | 78.0% [73.0, 82.3] | 85.0% [80.5, 88.6] | 0.699 [0.644, 0.766] | 0/300 |
| dense_floor_0.1 | dense 0.1 | 62.3% [56.7, 67.6] | 78.0% [73.0, 82.3] | 85.0% [80.5, 88.6] | 0.699 [0.644, 0.766] | 0/300 |
| hybrid | dense 0.55 | 64.7% [59.1, 69.9] | 80.7% [75.8, 84.7] | 87.0% [82.7, 90.3] | 0.727 [0.661, 0.808] | 0/300 |
| hybrid_rerank | dense 0.55, rerank -10.0 | 69.7% [64.2, 74.6] | 81.7% [76.9, 85.6] | 86.3% [82.0, 89.8] | 0.752 [0.697, 0.823] | 0/300 |
| hybrid_rerank_no_floor | dense 0.55, rerank -1000.0 | 69.7% [64.2, 74.6] | 81.7% [76.9, 85.6] | 86.3% [82.0, 89.8] | 0.752 [0.697, 0.823] | 0/300 |

Mailbox-clustered bootstrap 95% CIs (percent; questions in one mailbox are not independent):

| rung | hit@1 | hit@4 | hit@10 |
|---|---|---|---|
| baseline | [41.6, 59.2] | [61.0, 75.6] | [66.7, 82.8] |
| dense | [56.5, 69.5] | [72.5, 84.6] | [81.2, 89.4] |
| dense_floor_0.1 | [56.5, 69.5] | [72.5, 84.6] | [81.2, 89.4] |
| hybrid | [56.9, 73.7] | [74.8, 88.3] | [81.8, 92.9] |
| hybrid_rerank | [63.4, 77.3] | [75.9, 88.8] | [81.1, 91.4] |
| hybrid_rerank_no_floor | [63.4, 77.3] | [75.9, 88.8] | [81.1, 91.4] |

Paired differences (A - B, percentage points; same questions, mailbox-clustered bootstrap 95% CI). An interval that excludes 0 is a real difference on this sample:

| A vs B | hit@1 | hit@10 | MRR@10 (x100) |
|---|---|---|---|
| dense vs baseline | +12.7 [+6.8, +17.6] | +10.0 [+4.2, +16.5] | +11.8 [+7.0, +16.4] |
| hybrid vs dense | +2.3 [-0.7, +5.8] | +2.0 [+0.0, +4.5] | +2.8 [+0.8, +5.5] |
| hybrid_rerank vs hybrid | +5.0 [-0.5, +10.2] | -0.7 [-2.6, +1.0] | +2.5 [-0.9, +5.9] |
| hybrid_rerank vs dense | +7.3 [+4.0, +10.7] | +1.3 [-1.2, +3.5] | +5.4 [+2.8, +7.9] |

## Security and cost

| rung | leaks: non-reader questions | leaks: non-reader exact-content probes | all non-reader attempts | NoFilter control leaks (must be > 0) | hidden-doc diffs, exact-score ties ignored (strict order) | control: same corpus ingested reversed | owner latency p50 / p95 ms | ingest s |
|---|---|---|---|---|---|---|---|---|
| baseline | 0/900 | 0/180 | 0/1080 (upper 0.35%) | 100/100 | 0/75 (strict 37) | 0/25 (strict 17) | 144.0 / 158.5 | 18 |
| dense | 0/900 | 0/180 | 0/1080 (upper 0.35%) | 100/100 | 0/75 (strict 26) | 0/25 (strict 11) | 149.7 / 166.5 | 376 |
| dense_floor_0.1 | 0/900 | 0/180 | 0/1080 (upper 0.35%) | 100/100 | 0/75 (strict 26) | 0/25 (strict 11) | 151.6 / 176.5 | 16 |
| hybrid | 0/900 | 0/180 | 0/1080 (upper 0.35%) | 100/100 | 34/75 (strict 35) | 14/25 (strict 14) | 703.9 / 824.4 | 1006 |
| hybrid_rerank | 0/900 | 0/180 | 0/1080 (upper 0.35%) | 100/100 | 1/75 (strict 1) | 1/25 (strict 12) | 1004.7 / 1447.2 | 17 |
| hybrid_rerank_no_floor | 0/900 | 0/180 | 0/1080 (upper 0.35%) | 100/100 | 3/75 (strict 3) | 1/25 (strict 13) | 1066.9 / 3735.7 | 16 |

Environment: macOS-26.6.2-arm64-arm-64bit, Python 3.12.13, fastembed 0.9.0, qdrant-client 1.19.1. Seed 20261010. Latency is end-to-end `retrieve()` (includes the audit write and the unfiltered denied-count search) on a shared laptop, one run: orders of magnitude only.
