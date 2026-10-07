#!/usr/bin/env bash
set -euo pipefail
mode="${1:---plan}"
region="${AWS_REGION:-us-east-2}"
profile="${AWS_PROFILE:-permission-rag-demo}"
service="${AWS_DEMO_SERVICE:-permission-rag-demo}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "$mode" == "--plan" ]]; then
  cat <<PLAN
AWS Lightsail demo (no AWS calls made)
Region: $region | Profile: $profile | Service: $service
Build and smoke-test the synthetic, retrieval-only linux/amd64 image.
Create one Nano node (0.25 shared vCPU, 512 MB RAM), upload image privately,
and deploy with a managed HTTPS endpoint. Smoke-test the live endpoint.
Listed rate: \$7/month plus transfer overages/taxes; credits may offset charges.
Disabling the service does NOT stop charges. Delete it to stop hosting charges.
Audit files are ephemeral; replacement/redeployment can discard demo history.
Run --apply to create/update the service after reviewing this plan.
PLAN
  exit 0
fi
[[ "$mode" == "--apply" ]] || { echo 'Usage: deploy.sh [--plan|--apply]' >&2; exit 2; }
[[ "$region" == "us-east-2" ]] || { echo 'This AWS project is restricted to us-east-2.' >&2; exit 2; }
[[ "$service" =~ ^[a-z][a-z0-9-]{2,35}$ ]] || { echo 'Invalid service name.' >&2; exit 2; }
cd "$root"
for tool in aws docker python3 git lightsailctl; do
  command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done
[[ -z "$(git status --porcelain)" ]] || { echo 'Deploy requires a clean committed checkout.' >&2; exit 1; }
# lightsailctl uses the Docker API directly and does not resolve CLI contexts.
# Preserve an explicit host; otherwise pass the active context endpoint.
export DOCKER_HOST="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}"
aws_cmd=(aws --region "$region" --profile "$profile")
"${aws_cmd[@]}" sts get-caller-identity --query Account --output text
work="$(mktemp -d)"; container=""
cleanup() {
  if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi
  rm -rf "$work"
}
trap cleanup EXIT
# Read-only inventory before mutations; never mistake API errors for absence.
"${aws_cmd[@]}" lightsail get-container-services > "$work/services.json"
python3 - "$work/services.json" "$service" <<'PY'
import json, sys
matches=[s for s in json.load(open(sys.argv[1]))['containerServices'] if s['containerServiceName']==sys.argv[2]]
if matches:
    s=matches[0]
    tags={t['key']:t['value'] for t in s.get('tags',[])}
    if tags.get('Project')!='permission-aware-rag-demo' or s['power']!='nano' or s['scale']!=1 or s.get('isDisabled'):
        raise SystemExit('Existing service is unowned, disabled or has unexpected capacity; inspect manually.')
PY
tag="$(git rev-parse HEAD)"; image="permission-rag-demo:$tag"
docker build --platform linux/amd64 -f deploy/aws/Dockerfile -t "$image" .
container="$(docker run -d --platform linux/amd64 --memory 512m --cpus .25 -p 127.0.0.1::8080 "$image")"
port="$(docker port "$container" 8080/tcp | sed 's/.*://')"
python3 deploy/aws/smoke.py "http://127.0.0.1:$port"
docker rm -f "$container" >/dev/null; container=""
exists="$(python3 - "$work/services.json" "$service" <<'PY'
import json,sys
print(any(s['containerServiceName']==sys.argv[2] for s in json.load(open(sys.argv[1]))['containerServices']))
PY
)"
if [[ "$exists" == "False" ]]; then
  "${aws_cmd[@]}" lightsail create-container-service --service-name "$service" --power nano --scale 1 --tags key=Project,value=permission-aware-rag-demo > "$work/created.json"
fi
# Wait at most 20 minutes; all operations stay in the selected Region.
ready=false
for ((attempt=0; attempt<80; attempt++)); do
  state="$("${aws_cmd[@]}" lightsail get-container-services --service-name "$service" --query 'containerServices[0].state' --output text)"
  if [[ "$state" == "READY" || "$state" == "RUNNING" ]]; then ready=true; break; fi
  [[ "$state" != "DELETING" ]] || { echo 'Service is being deleted.' >&2; exit 1; }
  sleep 15
done
[[ "$ready" == true ]] || { echo 'Service did not become ready; inspect Lightsail. Charges may continue.' >&2; exit 1; }
"${aws_cmd[@]}" lightsail push-container-image --service-name "$service" --label "git-${tag:0:12}" --image "$image" > "$work/push.txt"
cat "$work/push.txt"
"${aws_cmd[@]}" lightsail get-container-images --service-name "$service" > "$work/images.json"
python3 - "$work/images.json" "$service" "$tag" deploy/aws/deployment.json "$work/deployment.json" <<'PY'
import json,sys
images=json.load(open(sys.argv[1]))['containerImages']
prefix=f':{sys.argv[2]}.git-{sys.argv[3][:12]}.'
matching=[i['image'] for i in images if i['image'].startswith(prefix)]
if not matching:raise SystemExit('Uploaded image not found; no deployment applied.')
image=max(matching,key=lambda i:int(i.rsplit('.',1)[1]))
d=json.load(open(sys.argv[4]));d['serviceName']=sys.argv[2];d['containers']['workbench']['image']=image
json.dump(d,open(sys.argv[5],'w'))
PY
version="$("${aws_cmd[@]}" lightsail create-container-service-deployment --cli-input-json "file://$work/deployment.json" --query 'containerService.nextDeployment.version' --output text)"
[[ "$version" =~ ^[0-9]+$ ]] || { echo 'No deployment version returned; inspect Lightsail.' >&2; exit 1; }
ready=false
for ((attempt=0; attempt<80; attempt++)); do
  "${aws_cmd[@]}" lightsail get-container-service-deployments --service-name "$service" > "$work/deployments.json"
  state="$(python3 - "$work/deployments.json" "$version" <<'PY'
import json,sys
print(next((d['state'] for d in json.load(open(sys.argv[1]))['deployments'] if d['version']==int(sys.argv[2])),'MISSING'))
PY
)"
  if [[ "$state" == "ACTIVE" ]]; then ready=true; break; fi
  [[ "$state" != "FAILED" ]] || { echo 'Deployment failed; inspect Lightsail. Charges continue until service deletion.' >&2; exit 1; }
  sleep 15
done
[[ "$ready" == true ]] || { echo 'Deployment timed out; inspect Lightsail. Charges may continue.' >&2; exit 1; }
url="$("${aws_cmd[@]}" lightsail get-container-services --service-name "$service" --query 'containerServices[0].url' --output text)"
python3 deploy/aws/smoke.py "$url"
printf 'Demo endpoint: %s\n' "$url"
