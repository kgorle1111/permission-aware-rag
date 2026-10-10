# AWS demo deployment

[Open the live demo](https://permission-rag-demo.b9hphyfz7skjm.us-east-2.cs.amazonlightsail.com/). Verified 2026-10-10 on
Lightsail deployment **version 2**, from merged commit
`b65e0c420d464bc248bbb35f8669cf085fa0d7cc`. PRs #6–#12 are merged. HTTPS UI,
retrieval-only operation, 20 permission probes, unknown-role denial and all four
roles' JSON/CSV audit privacy checks passed. [Release evidence](../../evals/results/2026-10-10-aws-release/table.md)
records the image, source tree, CI results and limits. The user chose to keep the
portfolio service running; capacity remains one Nano node. Version 1's private
image and deployment specification were retained for rollback.

This image includes the current reference ACL/privacy implementation. It hosts
the synthetic stdlib workbench, not the separate FastAPI/Qdrant platform or its
scale benchmark. The reference in-memory index is rebuilt on startup; platform
persisted-index upgrades are verified separately in local and CI tests.

One **Lightsail Nano container node** in **us-east-2** serves the synthetic
underwriting workbench over managed HTTPS. It uses predefined demo roles and
retrieval-only answers. Role selection demonstrates permissions; it does not
provide real-user authentication. Use synthetic questions. No model key, live
connector, user credentials or previous audit files enter the image.

Audit history is local and ephemeral: replacement/redeployment can discard it.
This is a portfolio demo, not production hosting or a scale benchmark.

## Setup and cost

AWS's new experience supports Lightsail. The project is on the Free plan;
credits and plan expiry are project-specific. The listed Nano rate is **$7/month**
for one node (0.25 shared vCPU, 512 MB RAM), plus applicable transfer overages and
taxes. Disabling the service does **not** stop billing; delete it to stop hosting
charges. Do not assume credits make hosting free indefinitely.

Sources checked 2026-10-07: [supported services](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html),
[pricing](https://aws.amazon.com/lightsail/pricing/),
[container capacity, billing and HTTPS](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-container-services.html).
The earlier App Runner templates were removed: they cannot serve this new project.

Install AWS CLI v2, Docker, Python 3, Git and
[lightsailctl](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-install-software.html)
(`brew install aws/tap/lightsailctl` on macOS). Authenticate with browser login:

```bash
aws login --region us-east-2 --profile permission-rag-demo
aws sts get-caller-identity --profile permission-rag-demo
bash deploy/aws/deploy.sh --plan
AWS_PROFILE=permission-rag-demo AWS_REGION=us-east-2 bash deploy/aws/deploy.sh --apply
```

The default plan makes no network calls. Apply requires a clean checkout, checks
identity and existing service ownership/capacity, builds a nonroot linux/amd64
image and smokes it locally under Nano resource limits before creating resources.
It passes the active Docker context endpoint to the upload helper (including
Colima/Docker Desktop sockets). It uploads to Lightsail's private image storage, deploys `deployment.json`, waits
for that exact deployment version and smokes the live HTTPS URL. It does not push
Git, upgrade the project, enable auto deployment, or call a paid model.
On failure, inspect the service and its deployment/container logs; created
resources can continue accruing charges. No automatic deletion runs on failure.

## Remove hosting

This removes the demo and its ephemeral history. Review the service name first:

```bash
aws lightsail get-container-services --region us-east-2 --profile permission-rag-demo
aws lightsail delete-container-service --service-name permission-rag-demo \
  --region us-east-2 --profile permission-rag-demo
```

Delete only the service you created. Do not disable it as a substitute for
removal. No custom domain or separate ECR/CloudFormation resources are required.
