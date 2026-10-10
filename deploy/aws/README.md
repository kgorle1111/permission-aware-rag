# AWS demo deployment

[Open the live demo](https://permission-rag-demo.b9hphyfz7skjm.us-east-2.cs.amazonlightsail.com/). Verified 2026-10-07 on
Lightsail deployment version 1: HTTPS UI, allowed retrieval, forbidden-document
check, retrieval-only answers and auditor query redaction passed. The junior
claims workflow was also verified in the browser. The user chose to keep the
portfolio service running. Deployment 1 uses stable reference commit `2d2b0f9e862b`;
the hierarchy and scale changes in [PR #6](https://github.com/kgorle1111/permission-aware-rag/pull/6) are not deployed yet.

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
