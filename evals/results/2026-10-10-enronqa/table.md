# EnronQA permission-aware retrieval eval (2026-10-10)

Dataset `MichaelR207/enron_qa_0922` @ `c0b3a9190fd9` (test split), seed 20261010. 24 mailboxes, 14542 emails, 78240 chunks (4167876 words), 600 questions. Engine: `app/permission_rag.py`, BM25 over the visible set, 80-word chunks.
Median owner query 67 ms (p95 155 ms) at this index size.

## Leaks (lower is better)

| Attempt type | Result |
|---|---|
| Owner queries | 0/600 = 0.00% (Wilson 95% CI 0.00-0.64%) |
| Non-owner, EnronQA question (3 per question) | 0/1800 = 0.00% (Wilson 95% CI 0.00-0.21%) |
| Non-owner, exact 8-token content probe (3 per probe) | 0/1800 = 0.00% (Wilson 95% CI 0.00-0.21%) |
| **All attempts** | **0/4200 = 0.00% (Wilson 95% CI 0.00-0.09%)** |

Controls: probe text retrieves its own email as the owner at top-1 in 491/600 = 81.83% (Wilson 95% CI 78.55-84.71%); the planted `NoFilter` mutant leaks on 100/100 non-owner queries.

## Recall, querying as the owner (higher is better)

| k | With ACL | No-ACL baseline |
|---|---|---|
| hit@1 | 73.0% (Wilson 69.3-76.4; mailbox-bootstrap 66.2-79.7) | 67.8% (Wilson 64.0-71.4; mailbox-bootstrap 61.1-74.7) |
| hit@4 | 91.7% (Wilson 89.2-93.6; mailbox-bootstrap 89.0-94.4) | 87.5% (Wilson 84.6-89.9; mailbox-bootstrap 83.9-90.5) |
| hit@10 | 95.8% (Wilson 93.9-97.2; mailbox-bootstrap 93.7-97.2) | 93.3% (Wilson 91.0-95.1; mailbox-bootstrap 90.2-95.4) |

## Citation validity (paid LLM step)

Not measured: not run (--no-llm or --llm-n 0).
