#!/usr/bin/env bash
set -euo pipefail

# Unified dual-cluster deployment & config reconciliation script for agent-runtime + coding-agent.
# Usage:
#   ./scripts/deploy_clusters.sh --clusters ar-node-1,cloudnium_cloud --ar-tag main-cfd46463 --ca-tag main-73d0ebb --mode ghcr --sync-gemini4-profiles

CLUSTERS="ar-node-1,cloudnium_cloud"
AR_TAG=""
CA_TAG=""
MODE="ghcr"
NAMESPACE="agent-runtime"
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

sync_ghcr_secret() {
  local host="$1"
  if [[ -z "${GHCR_USER:-}" || -z "${GHCR_TOKEN:-}" ]]; then
    return 0
  fi
  echo "[$host] Syncing secret/ghcr-pull-secret in namespace $NAMESPACE"
  local docker_cfg
  docker_cfg=$(python3 -c 'import base64,json,os; u=os.environ["GHCR_USER"]; p=os.environ["GHCR_TOKEN"]; auth=base64.b64encode(f"{u}:{p}".encode()).decode(); print(json.dumps({"auths":{"ghcr.io":{"username":u,"password":p,"auth":auth}}}))')
  printf '%s' "$docker_cfg" | ssh "$host" "sudo k3s kubectl -n '$NAMESPACE' create secret generic ghcr-pull-secret --from-file=.dockerconfigjson=/dev/stdin --type=kubernetes.io/dockerconfigjson --dry-run=client -o yaml | sudo k3s kubectl apply -f -" >/dev/null
}

sync_profiles_cm() {
  local host="$1"
  echo "[$host] Syncing agent-runtime-model-profiles & agent-runtime-model-allowlist"
  ssh "$host" "sudo k3s kubectl -n '$NAMESPACE' create configmap agent-runtime-model-profiles --from-file=model-profiles.json=/dev/stdin --dry-run=client -o yaml | sudo k3s kubectl apply -f -" \
    < "$AR_ROOT/deploy/dev/model-profiles.json" >/dev/null
  ssh "$host" "sudo k3s kubectl -n '$NAMESPACE' create configmap agent-runtime-model-allowlist --from-file=default-tenant-policies.json=/dev/stdin --dry-run=client -o yaml | sudo k3s kubectl apply -f -" \
    < "$AR_ROOT/deploy/dev/default-tenant-policies.json" >/dev/null
}

build_local_on_host() {
  local host="$1"
  echo "[$host] Streaming git archives for local build (AR=$AR_TAG, CA=$CA_TAG)"
  ssh "$host" 'rm -rf /tmp/ar-deploy-build && mkdir -p /tmp/ar-deploy-build/agent-runtime /tmp/ar-deploy-build/coding-agent'
  git -C "$AR_ROOT" archive HEAD | gzip -1 | ssh "$host" 'tar -xzf - -C /tmp/ar-deploy-build/agent-runtime'
  git -C "$CA_ROOT" archive HEAD | gzip -1 | ssh "$host" 'tar -xzf - -C /tmp/ar-deploy-build/coding-agent'
  ssh "$host" "set -euo pipefail
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
  sudo DOCKER_BUILDKIT=1 docker build -f deploy/images/go.Dockerfile --build-arg \"MODULE=\$2\" --build-arg \"MAIN=\$3\" -t \"ghcr.io/agentruntime/\$1:$AR_TAG\" .
done
for spec in \
  'worker agentruntime-worker' \
  'memory agentruntime-memory' \
  'learning agentruntime-learning'
do
  set -- \$spec
  sudo DOCKER_BUILDKIT=1 docker build -f deploy/images/python.Dockerfile --build-arg \"PACKAGE=\$2\" -t \"ghcr.io/agentruntime/\$1:$AR_TAG\" .
done
sudo DOCKER_BUILDKIT=1 docker build -f /tmp/ar-deploy-build/coding-agent/deploy/Dockerfile -t \"ghcr.io/agentruntime/coding-web:$CA_TAG\" /tmp/ar-deploy-build
sudo docker save \
  ghcr.io/agentruntime/task-service:$AR_TAG \
  ghcr.io/agentruntime/model-gateway:$AR_TAG \
  ghcr.io/agentruntime/egress-gateway:$AR_TAG \
  ghcr.io/agentruntime/pkg-mirror:$AR_TAG \
  ghcr.io/agentruntime/sandbox-service:$AR_TAG \
  ghcr.io/agentruntime/crawler-gateway:$AR_TAG \
  ghcr.io/agentruntime/worker:$AR_TAG \
  ghcr.io/agentruntime/memory:$AR_TAG \
  ghcr.io/agentruntime/learning:$AR_TAG \
  ghcr.io/agentruntime/coding-web:$CA_TAG | sudo k3s ctr images import -
rm -rf /tmp/ar-deploy-build
"
}

rollout_cluster() {
  local host="$1"
  echo "[$host] Deploying AR=$AR_TAG, CA=$CA_TAG (mode=$MODE)"
  sync_ghcr_secret "$host"
  if [[ "$SYNC_GEMINI4" -eq 1 ]]; then
    sync_profiles_cm "$host"
  fi
  if [[ "$MODE" == "local-build" ]]; then
    build_local_on_host "$host"
  fi

  sed "s/__TAG__/${AR_TAG}/g" "$AR_ROOT/deploy/cluster/wave4-crawler-and-wave2-evolution.yaml" | \
    ssh "$host" 'sudo k3s kubectl apply -f -' >/dev/null

  ssh "$host" "set -euo pipefail
patch_dep() {
  local dep=\"\$1\" img=\"\$2\" ver=\"\$3\"
  if ! sudo k3s kubectl -n '$NAMESPACE' get deploy \"\$dep\" >/dev/null 2>&1; then
    return 0
  fi
  local cname
  cname=\$(sudo k3s kubectl -n '$NAMESPACE' get deploy \"\$dep\" -o jsonpath='{.spec.template.spec.containers[0].name}')
  sudo k3s kubectl -n '$NAMESPACE' patch deploy \"\$dep\" --type=strategic -p \"{\\\"spec\\\":{\\\"template\\\":{\\\"spec\\\":{\\\"containers\\\":[{\\\"name\\\":\\\"\$cname\\\",\\\"image\\\":\\\"\$img\\\",\\\"env\\\":[{\\\"name\\\":\\\"OTEL_SERVICE_VERSION\\\",\\\"value\\\":\\\"\$ver\\\"}]}]}}}}\" >/dev/null
}
for dep in ar-agent-runtime-worker-decision ar-agent-runtime-worker-model ar-agent-runtime-worker-program ar-agent-runtime-worker-task ar-agent-runtime-worker-workspace; do
  patch_dep \"\$dep\" \"ghcr.io/agentruntime/worker:$AR_TAG\" \"$AR_TAG\"
done
patch_dep ar-agent-runtime-task-service    \"ghcr.io/agentruntime/task-service:$AR_TAG\"    \"$AR_TAG\"
patch_dep ar-agent-runtime-model-gateway   \"ghcr.io/agentruntime/model-gateway:$AR_TAG\"   \"$AR_TAG\"
patch_dep ar-agent-runtime-egress-gateway  \"ghcr.io/agentruntime/egress-gateway:$AR_TAG\"  \"$AR_TAG\"
patch_dep ar-agent-runtime-pkg-mirror      \"ghcr.io/agentruntime/pkg-mirror:$AR_TAG\"      \"$AR_TAG\"
patch_dep ar-agent-runtime-sandbox-service \"ghcr.io/agentruntime/sandbox-service:$AR_TAG\" \"$AR_TAG\"
patch_dep ar-agent-runtime-crawler-gateway \"ghcr.io/agentruntime/crawler-gateway:$AR_TAG\" \"$AR_TAG\"
patch_dep ar-agent-runtime-memory          \"ghcr.io/agentruntime/memory:$AR_TAG\"          \"$AR_TAG\"
patch_dep ar-agent-runtime-eval-dashboard  \"ghcr.io/agentruntime/learning:$AR_TAG\"        \"$AR_TAG\"
patch_dep coding-web                       \"ghcr.io/agentruntime/coding-web:$CA_TAG\"      \"$CA_TAG\"

for dep in ar-agent-runtime-crawler-gateway ar-agent-runtime-task-service ar-agent-runtime-model-gateway ar-agent-runtime-worker-program coding-web; do
  if sudo k3s kubectl -n '$NAMESPACE' get deploy \"\$dep\" >/dev/null 2>&1; then
    sudo k3s kubectl -n '$NAMESPACE' rollout status \"deploy/\$dep\" --timeout=240s
  fi
done
"
  echo "[$host] Verified cluster rollout ready (AR=$AR_TAG, CA=$CA_TAG)"
}

IFS=',' read -ra TARGETS <<< "$CLUSTERS"
for host in "${TARGETS[@]}"; do
  host="$(echo "$host" | xargs)"
  [[ -n "$host" ]] && rollout_cluster "$host"
done
