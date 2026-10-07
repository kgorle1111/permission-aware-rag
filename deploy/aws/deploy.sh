#!/usr/bin/env bash
set -euo pipefail

mode="${1:---plan}"
region="${AWS_REGION:-us-west-2}"
profile="${AWS_PROFILE:-permission-rag-demo}"
stack="${AWS_DEMO_STACK:-permission-rag-demo}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ "$mode" == "--plan" ]]; then
  cat <<PLAN
AWS demo deployment (no AWS calls made)
Region: $region | CLI profile: $profile | Stack prefix: $stack
1. Build and smoke-test the synthetic, retrieval-only linux/amd64 container.
2. Create the private ECR registry stack ($stack-registry).
3. Push an immutable image tag for the clean Git commit (reuse if already present).
4. Create/update the App Runner service stack ($stack-service).
5. Smoke-test the resulting managed HTTPS endpoint.
Capacity: 0.25 vCPU / 0.5 GB, min=1, max=1; automatic deployments disabled.
Estimate: ~\$2.56/month idle, ~\$14.24/month continuously active base compute,
plus ECR, CloudWatch and transfer in us-west-2. Credits/eligibility vary.
Run with --apply only after account sign-in and reviewing the cost and templates.
PLAN
  exit 0
fi
if [[ "$mode" != "--apply" ]]; then
  echo "Usage: AWS_PROFILE=permission-rag-demo AWS_REGION=us-west-2 bash deploy/aws/deploy.sh [--plan|--apply]" >&2
  exit 2
fi
if [[ ! "$stack" =~ ^[a-z][a-z0-9-]{2,35}$ ]]; then
  echo "AWS_DEMO_STACK must be 3–36 lowercase letters/digits/hyphens, starting with a letter." >&2
  exit 2
fi
cd "$root"
for command in aws docker python3 git; do
  command -v "$command" >/dev/null || { echo "Missing prerequisite: $command" >&2; exit 1; }
done
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Commit or isolate local changes before deploying; image tags identify a clean Git commit." >&2
  exit 1
fi
aws_cmd=(aws --region "$region" --profile "$profile")
"${aws_cmd[@]}" sts get-caller-identity --query Account --output text
for template in registry service; do
  "${aws_cmd[@]}" cloudformation validate-template --template-body "file://$root/deploy/aws/$template.json" >/dev/null
done
tag="$(git rev-parse HEAD)"
image="permission-rag-demo:$tag"
docker build --platform linux/amd64 -f deploy/aws/Dockerfile -t "$image" .
container=""
config_dir="$(mktemp -d)"
cleanup() {
  if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi
  rm -rf "$config_dir"
}
trap cleanup EXIT
container="$(docker run -d --platform linux/amd64 -p 127.0.0.1::8080 "$image")"
port="$(docker port "$container" 8080/tcp | sed 's/.*://')"
python3 deploy/aws/smoke.py "http://127.0.0.1:$port"
docker rm -f "$container" >/dev/null
container=""
"${aws_cmd[@]}" cloudformation deploy --stack-name "$stack-registry" --template-file deploy/aws/registry.json --no-fail-on-empty-changeset
repository="$("${aws_cmd[@]}" cloudformation describe-stacks --stack-name "$stack-registry" --query 'Stacks[0].Outputs[?OutputKey==`RepositoryUri`].OutputValue | [0]' --output text)"
repository_arn="$("${aws_cmd[@]}" cloudformation describe-stacks --stack-name "$stack-registry" --query 'Stacks[0].Outputs[?OutputKey==`RepositoryArn`].OutputValue | [0]' --output text)"
repository_name="${repository#*/}"
registry="${repository%%/*}"
# List and match exactly; fail on auth/network errors instead of treating them as an absent tag.
present="$("${aws_cmd[@]}" ecr list-images --repository-name "$repository_name" --query "imageIds[?imageTag=='$tag'].imageTag" --output text)"
if [[ -z "$present" ]]; then
  "${aws_cmd[@]}" ecr get-login-password | docker --config "$config_dir" login --username AWS --password-stdin "$registry"
  docker tag "$image" "$repository:$tag"
  docker --config "$config_dir" push "$repository:$tag"
fi
"${aws_cmd[@]}" cloudformation deploy --stack-name "$stack-service" --template-file deploy/aws/service.json --capabilities CAPABILITY_IAM --parameter-overrides "ImageUri=$repository:$tag" "RepositoryArn=$repository_arn" --no-fail-on-empty-changeset
url="$("${aws_cmd[@]}" cloudformation describe-stacks --stack-name "$stack-service" --query 'Stacks[0].Outputs[?OutputKey==`ServiceUrl`].OutputValue | [0]' --output text)"
python3 deploy/aws/smoke.py "$url"
printf 'Demo endpoint: %s\n' "$url"
