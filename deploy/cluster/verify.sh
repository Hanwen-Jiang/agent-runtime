#!/usr/bin/env bash
# 核对集群基线（docs/plan/cloud-deploy.md D-02 验收 1–3），install.sh 之后在机器上运行（KUBECONFIG 指向集群）：
#   1. 有 gvisor 与 kata 两个 RuntimeClass。
#   2. sandboxes 里起一个 gvisor Pod，内核是 gVisor 的；起一个 kata Pod，内核版本与宿主不同。
#   3. sandboxes 里的 Pod 访问外网与集群内服务都失败；Pod 里没有 ServiceAccount 令牌。
# 验收 4（经 Traefik 的 SSE 逐条到达）要 Runtime 装好之后跑：python scripts/sse_probe.py。
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
NS=sandboxes
IMAGE="${AR_VERIFY_IMAGE:-python:3.13-slim}"
fail=0
pass() { echo "通过：$1"; }
flunk() { echo "失败：$1" >&2; fail=1; }

# run_pod <名字> <RuntimeClass> <Python 代码>：起一个符合 restricted 的一次性 Pod，等它结束，打印输出。
run_pod() {
  local name="$1" class="$2" code
  code="$(sed 's/^/          /' <<<"$3")"
  kubectl -n "$NS" delete pod "$name" --ignore-not-found --wait >/dev/null
  kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $name
  labels: {app.kubernetes.io/managed-by: agentruntime-sandbox, agentruntime.io/verify: "true"}
spec:
  runtimeClassName: $class
  restartPolicy: Never
  automountServiceAccountToken: false
  enableServiceLinks: false
  securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: probe
      image: $IMAGE
      command: ["python", "-c"]
      args:
        - |
$code
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
      resources: {requests: {cpu: 100m, memory: 64Mi}, limits: {cpu: 500m, memory: 256Mi}}
EOF
  kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" --timeout=5m >/dev/null || true
  kubectl -n "$NS" logs "$name" || true
  kubectl -n "$NS" delete pod "$name" --wait=false >/dev/null
}

if kubectl get runtimeclass gvisor kata >/dev/null 2>&1; then pass "RuntimeClass gvisor 与 kata"; else flunk "RuntimeClass gvisor 与 kata"; fi

host_kernel="$(uname -r)"
gvisor_out="$(run_pod verify-gvisor gvisor "print(open('/proc/version').read())")"
# gVisor 自报的内核带 gvisor 字样（2026-10 的版本是 4.19.0-gvisor；更早的是 4.4.0），与宿主无关。
if grep -qi 'gvisor' <<<"$gvisor_out" && ! grep -qF "$host_kernel" <<<"$gvisor_out"; then
  pass "gvisor Pod 里是 gVisor 的内核"
else
  flunk "gvisor Pod 里是 gVisor 的内核（输出：$gvisor_out）"
fi

kata_kernel="$(run_pod verify-kata kata "import platform; print(platform.release())")"
if [[ -n "$kata_kernel" && "$kata_kernel" != "$host_kernel" ]]; then
  pass "kata Pod 的内核（$kata_kernel）与宿主（$host_kernel）不同"
else
  flunk "kata Pod 的内核与宿主不同（kata：${kata_kernel:-没有输出}，宿主：$host_kernel）"
fi

net_out="$(run_pod verify-network gvisor "$(cat <<'PY'
import os, socket
for host, port in [("1.1.1.1", 443), ("kubernetes.default.svc.cluster.local", 443), ("10.43.0.1", 443), ("pg-rw.data.svc.cluster.local", 5432)]:
    try:
        socket.create_connection((host, port), timeout=3).close()
        print("open", host)
    except OSError:
        print("blocked", host)
print("token", os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"))
PY
)")"
if grep -q '^blocked' <<<"$net_out" && ! grep -q '^open' <<<"$net_out"; then pass "沙箱连不上外网与集群内服务"; else flunk "沙箱连不上外网与集群内服务（$net_out）"; fi
if grep -q '^token False' <<<"$net_out"; then pass "沙箱里没有 ServiceAccount 令牌"; else flunk "沙箱里没有 ServiceAccount 令牌"; fi

exit "$fail"
