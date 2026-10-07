# AWS demo deployment

This deploys the reference underwriting workbench to **AWS App Runner** through a
private ECR image. It serves synthetic documents with predefined demo roles,
managed HTTPS, and retrieval-only answers. No model key or production connector
is included. Demo role selection is a showcase, not real-user authentication;
use synthetic questions. Audit state is local to one container and can disappear
on replacement or deployment. A container restart retains its local audit files,
but that is not a cloud durability guarantee.

## Account setup

1. Create an AWS account using [advanced signup](https://docs.aws.amazon.com/accounts/latest/reference/getting-started.html).
   The limited-rollout simplified new signup does **not** support App Runner;
   [AWS lists it among unsupported services](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html).
   If you already used that signup, review the implications before activating
   advanced features; this can change spending controls and billing access.
2. Enable root MFA, then use an administrative IAM/federated identity for this
   deployment. Its permissions must cover CloudFormation, ECR, App Runner and IAM
   role creation/passing (including App Runner's service-linked role on first use).
3. Install [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
   (at least 2.32.0), Docker, Python 3 and Git. Authenticate locally:

   ```bash
   aws login --profile permission-rag-demo
   aws sts get-caller-identity --profile permission-rag-demo
   ```

   Choose `us-west-2` (Oregon) when prompted. For an IAM identity, `aws login`
   requires `SignInLocalDevelopmentAccess` in addition to deployment permissions.
   IAM Identity Center users can instead use `aws configure sso` / `aws sso login`.
   [AWS authentication documentation](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html).
4. Review account credit eligibility and create a billing budget notification.
   Budget alerts are not a spending cap.

## Cost reviewed 2026-10-07

One instance, **0.25 vCPU / 0.5 GB**, in `us-west-2`. With 730 hours/month:

| Scenario | App Runner compute estimate |
|---|---:|
| Idle | 0.5 × $0.007 × 730 = **$2.56/month** |
| Continuously active | Idle memory + 0.25 × $0.064 × 730 = **$14.24/month** |

These exclude ECR storage, CloudWatch logs, data transfer and taxes. AWS bills
active CPU in minimum one-minute periods. Automatic deployments are disabled;
images build locally. The instance limit bounds compute capacity, not total AWS
spend. Credits depend on your account and do not make this free indefinitely.
[Current AWS pricing](https://aws.amazon.com/apprunner/pricing/).

## Review, then deploy

Run from the repository root:

```bash
bash deploy/aws/deploy.sh --plan
# After reviewing templates, costs and account identity:
AWS_PROFILE=permission-rag-demo AWS_REGION=us-west-2 bash deploy/aws/deploy.sh --apply
```

The default `--plan` makes no AWS calls and needs no credentials. `--apply` checks
identity, validates templates, requires a clean Git checkout, builds the container
for `linux/amd64` and smoke-tests it before creating resources. It then creates an
ECR registry stack, pushes the image tagged by Git commit, and creates or updates
the service stack. Existing immutable image tags are reused. Finally it tests the
HTTPS endpoint. Cloud deployment still needs verification in a real account.

Files:

- `Dockerfile`: nonroot Python 3.12, explicit code-only copies. Root `.dockerignore`
  excludes secrets, private planning, previous audits and unrelated files.
- `registry.json`: private encrypted ECR repository with immutable tags. Retained
  on stack deletion, so images are not silently destroyed.
- `service.json`: narrowly scoped image-pull role, HTTPS App Runner service,
  HTTP health check, one instance maximum. Application has no AWS instance role.
- `smoke.py`: UI, allowed retrieval, forbidden-document check, no LLM, auditor-query
  redaction. No API key, paid model call or real corpus is needed.

The resulting URL is printed at the end. Add it to the README only after the live
smoke passes. The script does not push Git branches or configure automatic deploys.

## Pause, resume and remove

Find the service ARN in the `permission-rag-demo-service` CloudFormation outputs.
Use `aws apprunner pause-service --service-arn ...` when the demo is not needed,
and `aws apprunner resume-service --service-arn ...` before presenting it. Pausing
stops App Runner compute billing; other resources can still incur charges.

To remove hosting, delete the **service** stack and wait for deletion to finish.
Then delete the registry stack. The registry's retention policy leaves its images
in ECR: review and explicitly delete that repository if you want storage billing
to stop. These operations are manual; the deployment script has no deletion mode.
If deployment fails, inspect the task's CloudFormation events and clean up its
resources when appropriate. Do not delete unrelated stacks or repositories.

CloudWatch log retention is AWS's default until configured in your account;
set a short retention period for this synthetic showcase. Container access logs
are disabled, but AWS deployment/system logs still exist. This is a single-instance
portfolio demo, not a production deployment or the 100k-document scale benchmark.
