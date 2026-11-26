#!/usr/bin/env bash
set -euo pipefail

# Unified cluster deployment & config reconciliation script for agent-runtime + coding-agent.
# Usage:
#   ./scripts/deploy_clusters.sh --clusters ar-node-1 --ar-tag main-40e4e072 --ca-tag main-257eb4d --mode ghcr --sync-gemini4-profiles

CLUSTERS="ar-node-1"
AR_TAG=""
CA_TAG=""
MODE="ghcr"
NAMESPACE="agent-runtime"
AR_REGISTRY="${AR_REGISTRY:-ghcr.io/hanwen-jiang}"
CA_REGISTRY="${CA_REGISTRY:-ghcr.io/agentruntime}"
SSH_JUMP="${SSH_JUMP:-}"
VALUES_FILE="${VALUES_FILE:-/opt/ar-cluster/ar-values-cluster.yaml}"
RELEASE_NAME="${RELEASE_NAME:-ar}"
SYNC_GEMINI4=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AR_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CA_ROOT="${CA_ROOT:-$(cd "${AR_ROOT}/../coding-agent" && pwd)}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clusters) CLUSTERS="$2"; shift 2 ;;
    --ar-tag) AR_TAG="$2"; shift 2 ;;
    --ca-tag) CA_TAG="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --registry) AR_REGISTRY="$2"; shift 2 ;;
    --ssh-jump) SSH_JUMP="$2"; shift 2 ;;
    --values-file) VALUES_FILE="$2"; shift 2 ;;
    --release) RELEASE_NAME="$2"; shift 2 ;;
    --sync-gemini4-profiles) SYNC_GEMINI4=1; shift 1 ;;
    -h|--help)
      sed -n '3,12p' "$0"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$MODE" != "ghcr" && "$MODE" != "local-build" ]]; then
  echo "--mode must be 'ghcr' or 'local-build'" >&2
  exit 2
fi

if [[ -z "$AR_TAG" ]]; then
  AR_TAG="main-$(git -C "$AR_ROOT" rev-parse --short=8 HEAD)"
fi
if [[ -z "$CA_TAG" ]]; then
  CA_TAG="main-$(git -C "$CA_ROOT" rev-parse --short=7 HEAD)"
fi

remote_sh() {
  local host="$1"; shift
  local cmd="$*"
  if [[ -n "$SSH_JUMP" ]]; then
    local target="$host"
    [[ "$host" == "ar-node-1" ]] && target="root@10.90.0.11"
    local b64
    b64=$(printf '%s' "$cmd" | base64)
    ssh -o ConnectTimeout=25 -o ServerAliveInterval=10 "$SSH_JUMP" "ssh -o BatchMode=yes -o ConnectTimeout=10 $target 'echo $b64 | base64 -d | bash'"
  else
    ssh "$host" "$cmd"
  fi
}

sync_ghcr_secret() {
  local host="$1"
  if [[ -z "${GHCR_USER:-}" || -z "${GHCR_TOKEN:-}" ]]; then
    return 0
  fi
  echo "[$host] Syncing secret/ghcr-pull-secret in namespace $NAMESPACE"
  local docker_cfg b64
  docker_cfg=$(python3 -c 'import base64,json,os; u=os.environ["GHCR_USER"]; p=os.environ["GHCR_TOKEN"]; auth=base64.b64encode(f"{u}:{p}".encode()).decode(); print(json.dumps({"auths":{"ghcr.io":{"username":u,"password":p,"auth":auth}}}))')
  b64=$(printf '%s' "$docker_cfg" | base64)
  remote_sh "$host" "echo '$b64' | base64 -d | sudo k3s kubectl -n '$NAMESPACE' create secret generic ghcr-pull-secret --from-file=.dockerconfigjson=/dev/stdin --type=kubernetes.io/dockerconfigjson --dry-run=client -o yaml | sudo k3s kubectl apply -f -" >/dev/null
}

sync_profiles_cm() {
  local host="$1"
  echo "[$host] Syncing agent-runtime-model-profiles & agent-runtime-model-allowlist"
  local b64p b64a
  b64p=$(base64 < "$AR_ROOT/deploy/dev/model-profiles.json")
  b64a=$(base64 < "$AR_ROOT/deploy/dev/default-tenant-policies.json")
  remote_sh "$host" "echo '$b64p' | base64 -d | sudo k3s kubectl -n '$NAMESPACE' create configmap agent-runtime-model-profiles --from-file=model-profiles.json=/dev/stdin --dry-run=client -o yaml | sudo k3s kubectl apply -f -" >/dev/null
  remote_sh "$host" "echo '$b64a' | base64 -d | sudo k3s kubectl -n '$NAMESPACE' create configmap agent-runtime-model-allowlist --from-file=tenant-policies.json=/dev/stdin --dry-run=client -o yaml | sudo k3s kubectl apply -f -" >/dev/null
}

build_local_on_host() {
  local host="$1"
  echo "[$host] Streaming git archives for local build (AR=$AR_TAG, CA=$CA_TAG)"
  remote_sh "$host" 'rm -rf /tmp/ar-deploy-build && mkdir -p /tmp/ar-deploy-build/agent-runtime /tmp/ar-deploy-build/coding-agent'
  if [[ -n "$SSH_JUMP" ]]; then
    local target="$host"
    [[ "$host" == "ar-node-1" ]] && target="root@10.90.0.11"
    git -C "$AR_ROOT" archive HEAD | gzip -1 | ssh -o ConnectTimeout=25 "$SSH_JUMP" "ssh -o BatchMode=yes $target 'tar -xzf - -C /tmp/ar-deploy-build/agent-runtime'"
    git -C "$CA_ROOT" archive HEAD | gzip -1 | ssh -o ConnectTimeout=25 "$SSH_JUMP" "ssh -o BatchMode=yes $target 'tar -xzf - -C /tmp/ar-deploy-build/coding-agent'"
  else
    git -C "$AR_ROOT" archive HEAD | gzip -1 | ssh "$host" 'tar -xzf - -C /tmp/ar-deploy-build/agent-runtime'
    git -C "$CA_ROOT" archive HEAD | gzip -1 | ssh "$host" 'tar -xzf - -C /tmp/ar-deploy-build/coding-agent'
  fi
  remote_sh "$host" "set -euo pipefail
export DOCKER_BUILDKIT=1
cd /tmp/ar-deploy-build/agent-runtime
for spec in \
  'task-service services/go-task-service ./cmd/task-service' \
  'model-gateway services/go-model-gateway ./cmd/model-gateway' \
  'egress-gateway services/go-egress-gateway ./cmd/egress-gateway' \
  'pkg-mirror services/go-pkg-mirror ./cmd/pkg-mirror' \
  'sandbox-service services/go-sandbox-service ./cmd/sandbox-service' \
  'crawler-gateway services/go-crawler-gateway ./cmd/crawler-gateway'
do
  set -- \$spec
  sudo DOCKER_BUILDKIT=1 docker build -f deploy/images/go.Dockerfile --build-arg \"MODULE=\$2\" --build-arg \"MAIN=\$3\" -t \"${AR_REGISTRY}/\$1:$AR_TAG\" .
done
for spec in \
  'worker agentruntime-worker' \
  'memory agentruntime-memory' \
  'learning agentruntime-learning'
do
  set -- \$spec
  sudo DOCKER_BUILDKIT=1 docker build -f deploy/images/python.Dockerfile --build-arg \"PACKAGE=\$2\" -t \"${AR_REGISTRY}/\$1:$AR_TAG\" .
done
sudo DOCKER_BUILDKIT=1 docker build -f /tmp/ar-deploy-build/coding-agent/deploy/Dockerfile -t \"${CA_REGISTRY}/coding-web:$CA_TAG\" /tmp/ar-deploy-build
sudo docker save \
  ${AR_REGISTRY}/task-service:$AR_TAG \
  ${AR_REGISTRY}/model-gateway:$AR_TAG \
  ${AR_REGISTRY}/egress-gateway:$AR_TAG \
  ${AR_REGISTRY}/pkg-mirror:$AR_TAG \
  ${AR_REGISTRY}/sandbox-service:$AR_TAG \
  ${AR_REGISTRY}/crawler-gateway:$AR_TAG \
  ${AR_REGISTRY}/worker:$AR_TAG \
  ${AR_REGISTRY}/memory:$AR_TAG \
  ${AR_REGISTRY}/learning:$AR_TAG \
  ${CA_REGISTRY}/coding-web:$CA_TAG | sudo k3s ctr images import -
rm -rf /tmp/ar-deploy-build
"
}

rollout_cluster() {
  local host="$1"
  echo "[$host] Deploying AR=$AR_TAG ($AR_REGISTRY), CA=$CA_TAG ($CA_REGISTRY) (mode=$MODE)"
  sync_ghcr_secret "$host"
  if [[ "$SYNC_GEMINI4" -eq 1 ]]; then
    sync_profiles_cm "$host"
  fi
  if [[ "$MODE" == "local-build" ]]; then
    build_local_on_host "$host"
  fi

  local b64_chart
  b64_chart=$(tar -czf - -C "$AR_ROOT/deploy/helm" agent-runtime | base64)
  remote_sh "$host" "set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
rm -rf /tmp/ar-chart && echo '$b64_chart' | base64 -d | tar -xzf - -C /tmp && mv /tmp/agent-runtime /tmp/ar-chart
if [[ -f '$VALUES_FILE' ]]; then
  helm upgrade '$RELEASE_NAME' /tmp/ar-chart -n '$NAMESPACE' -f '$VALUES_FILE' --set-string image.registry='$AR_REGISTRY' --set-string image.tag='$AR_TAG' --wait --timeout 5m
else
  echo \"Missing values file $VALUES_FILE; skipping helm upgrade\" >&2
  exit 1
fi
rm -rf /tmp/ar-chart
if sudo k3s kubectl -n '$NAMESPACE' get deploy coding-web >/dev/null 2>&1; then
  cname=\$(sudo k3s kubectl -n '$NAMESPACE' get deploy coding-web -o jsonpath='{.spec.template.spec.containers[0].name}')
  sudo k3s kubectl -n '$NAMESPACE' patch deploy coding-web --type=strategic -p \"{\\\"spec\\\":{\\\"template\\\":{\\\"spec\\\":{\\\"containers\\\":[{\\\"name\\\":\\\"\$cname\\\",\\\"image\\\":\\\"${CA_REGISTRY}/coding-web:$CA_TAG\\\",\\\"env\\\":[{\\\"name\\\":\\\"OTEL_SERVICE_VERSION\\\",\\\"value\\\":\\\"$CA_TAG\\\"}]}]}}}}\" >/dev/null
  sudo k3s kubectl -n '$NAMESPACE' rollout status deploy/coding-web --timeout=240s
fi
"
  echo "[$host] Verified Helm cluster rollout ready (AR=$AR_TAG, CA=$CA_TAG)"
}

IFS=',' read -ra TARGETS <<< "$CLUSTERS"
for host in "${TARGETS[@]}"; do
  host="$(echo "$host" | xargs)"
  [[ -n "$host" ]] && rollout_cluster "$host"
done
