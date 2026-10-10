# EnronQA permission-aware retrieval eval (2026-10-10)

Dataset `MichaelR207/enron_qa_0922` @ `c0b3a9190fd9` (test split), seed 20261010. 24 mailboxes, 14542 emails, 78240 chunks (4167876 words), 600 questions. Engine: `app/permission_rag.py`, BM25 over the visible set, 80-word chunks.
Median owner query 70 ms (p95 165 ms) at this index size.

## Permission structure (header participants)

Readers of an email = its mailbox owner plus every Sender/Recipients address that maps to one of the 150 users. EnronQA flattens To/Cc/Bcc into one `Recipients` field, so they are not separated. Address map: 134/150 users mapped from their dominant sent-folder sender address (6 have no sent emails, 10 fail the dominance rule, 0 dropped as ambiguous).

Readers per email: 1: 12355, 2: 1893, 3-5: 294, 6+: 0 (15.0% of emails have 2+ readers; mean 1.17, max 5; 1689 emails have a reader outside the sampled mailboxes).

## Leaks (lower is better)

| Attempt type | Result |
|---|---|
| Owner queries | 0/600 = 0.00% (Wilson 95% CI 0.00-0.64%) |
| Other allowed participants, EnronQA question (up to 3 per question) | 0/100 = 0.00% (Wilson 95% CI 0.00-3.70%) |
| Non-readers, EnronQA question (3 per question) | 0/1800 = 0.00% (Wilson 95% CI 0.00-0.21%) |
| Non-readers, exact 8-token content probe (3 per probe) | 0/1800 = 0.00% (Wilson 95% CI 0.00-0.21%) |
| **All attempts** | **0/4300 = 0.00% (Wilson 95% CI 0.00-0.09%)** |
| All non-reader attempts | 0/3600 = 0.00% (Wilson 95% CI 0.00-0.11%) |

Queries by non-readers that returned anything at all: 1800 (questions), 1773 (probes); the rest correctly returned nothing.

Controls (the oracle must see planted bugs):

- Probe text retrieves its own email for its owner at top-1: 480/600 = 80.00% (Wilson 95% CI 76.61-83.01%).
- `NoFilter` mutant (no ACL check): leaks on 100/100 = 100.00% (Wilson 95% CI 96.30-100.00%) non-reader queries.
- Off-by-one ACL mutant (each email also readable by the next email's readers), targeted probes: leaks on 300/300 = 100.00% (Wilson 95% CI 98.74-100.00%); same probes on the real engine: 0/300 = 0.00% (Wilson 95% CI 0.00-1.26%).
- Recipients-ignored mutant (readers = owner + sender): over-denies instead of leaking, so it shows as lost recall for legitimate readers: hit@4 0/42 vs 41/42 on the real engine.

## Recall (higher is better)

| k | Owner, with ACL | Other allowed participants, with ACL | No-ACL baseline (all questions) |
|---|---|---|---|
| hit@1 | 72.8% (Wilson 69.1-76.2; mailbox-bootstrap 66.0-79.6) | 87.0% (Wilson 79.0-92.2; mailbox-bootstrap 78.2-96.2) | 67.8% (Wilson 64.0-71.4; mailbox-bootstrap 61.1-74.7) |
| hit@4 | 91.5% (Wilson 89.0-93.5; mailbox-bootstrap 88.8-94.3) | 97.0% (Wilson 91.5-99.0; mailbox-bootstrap 94.8-100.0) | 87.5% (Wilson 84.6-89.9; mailbox-bootstrap 83.9-90.5) |
| hit@10 | 95.7% (Wilson 93.7-97.0; mailbox-bootstrap 93.4-97.0) | 99.0% (Wilson 94.6-99.8; mailbox-bootstrap 97.6-100.0) | 93.3% (Wilson 91.0-95.1; mailbox-bootstrap 90.2-95.4) |

Owner recall on only the questions whose email has another reader: hit@1 76.1% (Wilson 66.3-83.8; mailbox-bootstrap 63.4-90.9), hit@4 94.3% (Wilson 87.4-97.5; mailbox-bootstrap 91.3-97.8), hit@10 96.6% (Wilson 90.5-98.8; mailbox-bootstrap 92.5-100.0)

## Shared-email stratum (600 questions whose email has 2+ readers)

The random sample has few shared emails, so this stratum is drawn only from emails with 2+ readers (it is not representative of all questions).

| Attempt type | Result |
|---|---|
| Owner queries | 0/600 = 0.00% (Wilson 95% CI 0.00-0.64%) |
| Other allowed participants (up to 3 per question) | 0/697 = 0.00% (Wilson 95% CI 0.00-0.55%) |
| Non-readers (3 per question) | 0/1800 = 0.00% (Wilson 95% CI 0.00-0.21%) |
| **All attempts** | **0/3097 = 0.00% (Wilson 95% CI 0.00-0.12%)** |

| k | Owner | Other allowed participants | No-ACL baseline |
|---|---|---|---|
| hit@1 | 77.0% (Wilson 73.5-80.2; mailbox-bootstrap 74.0-84.1) | 83.6% (Wilson 80.7-86.2; mailbox-bootstrap 78.9-90.1) | 73.0% (Wilson 69.3-76.4; mailbox-bootstrap 68.7-80.1) |
| hit@4 | 92.2% (Wilson 89.7-94.1; mailbox-bootstrap 89.7-98.0) | 95.4% (Wilson 93.6-96.7; mailbox-bootstrap 93.6-99.3) | 89.7% (Wilson 87.0-91.9; mailbox-bootstrap 87.1-95.4) |
| hit@10 | 94.8% (Wilson 92.8-96.3; mailbox-bootstrap 92.9-99.2) | 97.0% (Wilson 95.4-98.0; mailbox-bootstrap 95.7-99.7) | 93.8% (Wilson 91.6-95.5; mailbox-bootstrap 91.7-97.2) |

## Comparison: single-owner ACL (mailbox owner only)

All attempts: 0/4200 = 0.00% (Wilson 95% CI 0.00-0.09%). Non-reader attempts: 0/3600 = 0.00% (Wilson 95% CI 0.00-0.11%).
Owner recall: hit@1 73.0% (Wilson 69.3-76.4; mailbox-bootstrap 66.2-79.7), hit@4 91.7% (Wilson 89.2-93.6; mailbox-bootstrap 89.0-94.4), hit@10 95.8% (Wilson 93.9-97.2; mailbox-bootstrap 93.7-97.2).

## Citation validity (paid LLM step)

Not measured: not run (--no-llm or --llm-n 0).
