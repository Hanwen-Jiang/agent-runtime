#!/usr/bin/env bash
set -euo pipefail

# Secret rotation helper for agent-runtime & coding-web (P2-1).
# Defaults to --dry-run; pass --apply to mutate Kubernetes Secrets and trigger rollout restarts.
# Security invariant: NEVER prints or logs any secret value.

APPLY=0
NAMESPACE="agent-runtime"
SSH_TARGET=""
KUBE_CONTEXT=""
TARGETS="crawler,admin,registry"

usage() {
  cat <<'EOF'
Usage: scripts/rotate_secrets.sh [OPTIONS]
  --dry-run                Print planned secret rotations without modifying the cluster (default)
  --apply                  Generate new random 256-bit tokens, patch Secrets, and restart workloads
  --namespace <ns>         Kubernetes namespace (default: agent-runtime)
  --ssh <host>             Run kubectl remotely via ssh <host>
  --context <ctx>          kubectl --context <ctx>
  --targets <csv>          Comma-separated groups: crawler,admin,registry (default: crawler,admin,registry)
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) APPLY=0; shift ;;
    --apply) APPLY=1; shift ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --ssh) SSH_TARGET="$2"; shift 2 ;;
    --context) KUBE_CONTEXT="$2"; shift 2 ;;
    --targets) TARGETS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

kctl() {
  local args=()
  if [[ -n "$KUBE_CONTEXT" ]]; then
    args+=(--context "$KUBE_CONTEXT")
  fi
  if [[ -n "$SSH_TARGET" ]]; then
    ssh "$SSH_TARGET" kubectl "${args[@]}" -n "$NAMESPACE" "$@"
  else
    kubectl "${args[@]}" -n "$NAMESPACE" "$@"
  fi
}

gen_token() {
  openssl rand -hex 32
}

has_target() {
  [[ ",${TARGETS}," == *",$1,"* ]]
}

rotate_crawler() {
  echo "[rotate:crawler] target secret/crawler-gateway-secret keys: token, headless-token, admin-token, searxng-secret"
  echo "[rotate:crawler] impacted deployments: ar-agent-runtime-crawler-gateway, ar-agent-runtime-crawler-headless, ar-agent-runtime-searxng, ar-agent-runtime-worker-{task,decision,model,program,workspace}"
  if [[ "$APPLY" -eq 0 ]]; then
    echo "[dry-run] skipping secret mutation and rollout restarts for crawler"
    return 0
  fi
  local t_gw t_hl t_adm t_sx
  t_gw="$(gen_token)"
  t_hl="$(gen_token)"
  t_adm="$(gen_token)"
  t_sx="$(gen_token)"
  kubectl_patch_json="{\"stringData\":{\"token\":\"${t_gw}\",\"headless-token\":\"${t_hl}\",\"admin-token\":\"${t_adm}\",\"searxng-secret\":\"${t_sx}\"}}"
  kctl patch secret crawler-gateway-secret --type=merge -p "$kubectl_patch_json" >/dev/null
  for dep in ar-agent-runtime-crawler-gateway ar-agent-runtime-crawler-headless ar-agent-runtime-searxng ar-agent-runtime-worker-task ar-agent-runtime-worker-decision ar-agent-runtime-worker-model ar-agent-runtime-worker-program ar-agent-runtime-worker-workspace; do
    kctl rollout restart "deployment/${dep}" >/dev/null 2>&1 || true
  done
  echo "[rotate:crawler] rotated secret/crawler-gateway-secret and restarted crawler/worker deployments"
}

rotate_admin() {
  echo "[rotate:admin] target secret/coding-admin keys: evolution-runner-token, coding-dance-token (preserves admin-email/password-hash & grafana credentials)"
  echo "[rotate:admin] impacted deployments: coding-web, ar-agent-runtime-evolution-runner"
  if [[ "$APPLY" -eq 0 ]]; then
    echo "[dry-run] skipping secret mutation and rollout restarts for admin"
    return 0
  fi
  local t_evo t_cd
  t_evo="$(gen_token)"
  t_cd="$(gen_token)"
  kctl patch secret coding-admin --type=merge -p "{\"stringData\":{\"evolution-runner-token\":\"${t_evo}\",\"coding-dance-token\":\"${t_cd}\"}}" >/dev/null
  for dep in coding-web ar-agent-runtime-evolution-runner; do
    kctl rollout restart "deployment/${dep}" >/dev/null 2>&1 || true
  done
  echo "[rotate:admin] rotated secret/coding-admin tokens and restarted coding-web/evolution-runner"
}

rotate_registry() {
  echo "[rotate:registry] target coordinated secret: coding-admin (registry-token) + task-service SecretStore/seed"
  echo "[rotate:registry] impacted workloads: ar-agent-runtime-task-service, coding-web, ar-agent-runtime-evolution-runner, CronJob/ar-self-evolution-cycle"
  if [[ "$APPLY" -eq 0 ]]; then
    echo "[dry-run] skipping registry-token mutation; note registry-token requires synchronized update in task-service SecretStore + coding-admin"
    return 0
  fi
  if ! kctl get secret agent-runtime-secrets >/dev/null 2>&1; then
    echo "[rotate:registry] secret/agent-runtime-secrets not found; aborting coordinated rotation" >&2
    return 1
  fi
  local t_reg
  t_reg="$(gen_token)"
  kctl patch secret coding-admin --type=merge -p "{\"stringData\":{\"registry-token\":\"${t_reg}\"}}" >/dev/null
  python3 - "$NAMESPACE" "$SSH_TARGET" "$KUBE_CONTEXT" "$t_reg" <<'PY'
import base64, json, subprocess, sys

ns, ssh_host, ctx, new_tok = sys.argv[1:5]
cmd = (["ssh", ssh_host] if ssh_host else []) + ["kubectl"] + (["--context", ctx] if ctx else []) + ["-n", ns]
raw = subprocess.check_output(cmd + ["get", "secret", "agent-runtime-secrets", "-o", "json"])
sec = json.loads(raw)
data = sec.get("data") or {}
seed_b64 = data.get("secrets.json")
if seed_b64:
    seed = json.loads(base64.b64decode(seed_b64).decode())
    plat = seed.setdefault("_platform", {})
    plat["registry/publish-token"] = new_tok
    data["secrets.json"] = base64.b64encode(json.dumps(seed).encode()).decode()
    sec["data"] = data
    for k in ("resourceVersion", "uid", "creationTimestamp", "managedFields"):
        sec.get("metadata", {}).pop(k, None)
    subprocess.run(cmd + ["apply", "-f", "-"], input=json.dumps(sec).encode(), check=True, stdout=subprocess.DEVNULL)
PY
  for dep in ar-agent-runtime-task-service coding-web ar-agent-runtime-evolution-runner; do
    kctl rollout restart "deployment/${dep}" >/dev/null 2>&1 || true
  done
  echo "[rotate:registry] rotated coordinated registry-token across coding-admin & agent-runtime-secrets and restarted dependent deployments"
}

echo "mode=$([[ "$APPLY" -eq 1 ]] && echo apply || echo dry-run) namespace=${NAMESPACE} target=${SSH_TARGET:-local} groups=${TARGETS}"
has_target crawler && rotate_crawler
has_target admin && rotate_admin
has_target registry && rotate_registry
