# AWS release verification — 2026-10-10

PRs [#6](https://github.com/kgorle1111/permission-aware-rag/pull/6) through
[#12](https://github.com/kgorle1111/permission-aware-rag/pull/12) are merged.
Lightsail deployment **2** is ACTIVE from merged commit
`b65e0c420d464bc248bbb35f8669cf085fa0d7cc`, with the exact reviewed tree
`10b62b146e8af928be77b6d2cae12de58bdee379`. [Structured report](report.json).

| Check | Actual result |
|---|---|
| Both PR12 Linux mutation campaigns | 1,688/1,708 killed, 20 reviewed equivalents; all other outcomes zero; both strict validators passed again in the parent under Python 3.12 |
| Hosted service | Same managed HTTPS endpoint, one Nano node in us-east-2; no capacity increase |
| Demo smoke | UI, permitted retrieval, forbidden-document denial, retrieval-only answer and query redaction passed |
| Extended permission probes | 20 passed: 12 positive controls and 8 denied targets across four roles; supplied body groups do not widen access |
| Unknown roles | Three rejected with HTTP 400 |
| JSON audit | Four role views passed: redacted queries, retained IDs/counts, own-user scope except the auditor's all-user metadata view |
| Independent CSV supplement | Four views passed: exact six columns, permitted doc#0 chunk IDs for each recorded user, correct scope, redaction, finite nonnegative time metadata and canonical nonnegative integer denial counts |
| Upgrade/privacy regression subset | 28 tests passed locally; platform durable-index upgrades are separate from the hosted reference demo |

The deployed environment contains HOST/PORT only; image environment and the
credential-excluding build allowlist were checked before invoking /ask. No model
key is configured. Demo role selection is not authentication. The hosted
in-memory index is rebuilt from the seven synthetic documents at startup; no
platform SQL/Qdrant migration or 100k-scale cloud deployment is claimed. Audit
history is ephemeral across deployment replacement.

The independent test agent reviewed the primary oracle against the source roles
and corpus. Its separate CSV check initially assumed document IDs and ISO times;
reference audit rows use chunk IDs and epoch timestamps. Original failure/script
were preserved and a schema-corrected variant was preregistered before execution.
The primary frozen script stayed unchanged; the corrected supplement passed both
locally and live. Scripts, preregistrations, rollout/rollback specification and
full CI artifacts remain in the maintainer's ignored archive. This is agent
verification, not sealed hidden evaluation or external-human L4 evidence.

Reproduce the public smoke without a model call:

```bash
python3 deploy/aws/smoke.py https://permission-rag-demo.b9hphyfz7skjm.us-east-2.cs.amazonlightsail.com/
```

The recorded extended checks cover IDs and metadata, not a text-content oracle,
production identity, live hierarchy/recovery, score isolation, durable audit or
concurrent performance. The historical scale reports retain their original source
pins and measurements. The article remains private and unpublished.
