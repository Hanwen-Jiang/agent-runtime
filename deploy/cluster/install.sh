#!/usr/bin/env bash
# 把一台新机器装成单节点 k3s 集群（docs/plan/cloud-deploy.md D-02），在机器上以 root 运行：
#   sudo deploy/cluster/install.sh
# 做的事（重复运行是安全的）：
#   1. gVisor：下载这个版本的包（runsc、containerd-shim-runsc-v1 与 gvisor-bin/），核对 sha512 后解到 /usr/local/bin。
#   2. k3s：配置（config.yaml、psa.yaml、containerd 模板、Traefik 配置）放好后按 versions.conf 的版本安装。
#   3. Kata（机器有 /dev/kvm 时）：kata-deploy 的 Helm Chart；没有 KVM 时跳过并提示，kata RuntimeClass 的 Pod 会起不来。
#   4. 工作区目录 /var/lib/agentruntime/workspaces（属主 10001）。
#   5. 集群里的基础清单：命名空间、沙箱的约束、RuntimeClass、工作区与业务对象的卷。
#   6. cert-manager、KEDA、CloudNativePG 与它的 Barman Cloud 插件。
# 之后：密钥（secrets/README.md）→ deploy/data/install.sh → Runtime 的 Chart（README.md 的步骤）。
# 版本都在 versions.conf，装之前核对；下载的东西都校验（k3s 的安装脚本自己核对二进制的 sha256）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=versions.conf
source "$HERE/versions.conf"

log() { printf '==> %s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "要以 root 运行"
[[ "$(uname -m)" == "x86_64" ]] || die "镜像只做了 amd64（deploy/images）"
command -v curl >/dev/null || die "要有 curl"
command -v sha512sum >/dev/null || die "要有 sha512sum"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- 1. gVisor ---
if ! command -v runsc >/dev/null || [[ "$(runsc --version 2>/dev/null | head -1)" != *"$GVISOR_RELEASE"* ]]; then
  log "安装 gVisor $GVISOR_RELEASE"
  # 2026-07 起一个版本一个包；runsc 在自己旁边找 gvisor-bin/，所以整包解到同一个目录。
  base="https://storage.googleapis.com/gvisor/releases/release/${GVISOR_RELEASE}/x86_64"
  curl -fsSL -o "$WORK/gvisor.tar.bz2" "$base/gvisor.tar.bz2"
  curl -fsSL -o "$WORK/gvisor.tar.bz2.sha512" "$base/gvisor.tar.bz2.sha512"
  (cd "$WORK" && sha512sum -c gvisor.tar.bz2.sha512)
  tar -xjf "$WORK/gvisor.tar.bz2" -C /usr/local/bin
  chmod 0755 /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1
fi

# --- 2. k3s ---
log "k3s 的配置"
install -d -m 0700 /etc/rancher/k3s
install -m 0600 "$HERE/k3s/config.yaml" /etc/rancher/k3s/config.yaml
install -m 0600 "$HERE/k3s/psa.yaml" /etc/rancher/k3s/psa.yaml
install -d /var/lib/rancher/k3s/agent/etc/containerd /var/lib/rancher/k3s/server/manifests
install -m 0644 "$HERE/k3s/config-v3.toml.tmpl" /var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.tmpl
install -m 0644 "$HERE/k3s/traefik-config.yaml" /var/lib/rancher/k3s/server/manifests/traefik-config.yaml
# 环境镜像仓库（D-17）的拉取凭据：设了 ENV_REGISTRY_USER、ENV_REGISTRY_PASSWORD 才写（与 SecretStore 的 sandbox/env-registry 相同）。
if [[ -n "${ENV_REGISTRY_USER:-}" && -n "${ENV_REGISTRY_PASSWORD:-}" ]]; then
  ( umask 077 && sed -e "s|\${ENV_REGISTRY_USER}|$ENV_REGISTRY_USER|" -e "s|\${ENV_REGISTRY_PASSWORD}|$ENV_REGISTRY_PASSWORD|" \
      "$HERE/k3s/registries.yaml.tmpl" > /etc/rancher/k3s/registries.yaml )
else
  log "没设 ENV_REGISTRY_USER、ENV_REGISTRY_PASSWORD：节点拉不了环境镜像仓库里的镜像（按项目定制的运行环境会退回 universal）"
fi

if ! command -v k3s >/dev/null || [[ "$(k3s --version | head -1)" != *"$K3S_VERSION"* ]]; then
  log "安装 k3s $K3S_VERSION"
  # 先下载到文件再执行，不直接把网上的脚本接给 sh。
  curl -fsSL -o "$WORK/k3s-install.sh" https://get.k3s.io
  INSTALL_K3S_VERSION="$K3S_VERSION" sh "$WORK/k3s-install.sh"
fi
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
log "等节点就绪"
until kubectl get nodes >/dev/null 2>&1; do sleep 2; done
kubectl wait --for=condition=Ready node --all --timeout=5m

command -v helm >/dev/null || die "要有 helm（https://helm.sh/docs/intro/install/，与 CLAUDE.md 的版本一致）"

# --- 3. Kata ---
if [[ -e /dev/kvm ]]; then
  log "安装 Kata $KATA_VERSION（kata-deploy）"
  helm upgrade --install kata-deploy oci://ghcr.io/kata-containers/kata-deploy-charts/kata-deploy \
    --version "$KATA_VERSION" --namespace kube-system --set k8sDistribution=k3s --wait --timeout 15m
  kubectl wait --for=jsonpath='{.metadata.labels.katacontainers\.io/kata-runtime}'=true node --all --timeout=15m
  # kata-deploy 会改 containerd 的模板；核对 gVisor 的 runsc 还在。
  grep -q 'runtimes.runsc' /var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.tmpl \
    || die "kata-deploy 改写 containerd 模板时丢了 runsc，见 README 的“Kata 与 gVisor 共存”"
  # 按项目定制的运行环境的构建（D-17）要特权跑 BuildKit：特权只能停在 VM 里，Kata 的 handler 要 privileged_without_host_devices。
  awk '/^[[:space:]]*\[/{s=($0 ~ /kata-clh-runtime-rs/)} s && /privileged_without_host_devices[[:space:]]*=[[:space:]]*true/{f=1} END{exit !f}' \
      /var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.tmpl /var/lib/rancher/k3s/agent/etc/containerd/config.toml 2>/dev/null \
    || die "Kata 的 handler 没有 privileged_without_host_devices = true：构建环境的特权会碰到宿主的设备，见 README 的“项目环境的构建”"
else
  log "没有 /dev/kvm：跳过 Kata（kata RuntimeClass 的沙箱起不来，租户策略别指到 kata）"
fi

# --- 4. 工作区 ---
install -d -o 10001 -g 10001 -m 0750 /var/lib/agentruntime/workspaces

# --- 5. 基础清单 ---
log "命名空间、沙箱约束、RuntimeClass、卷"
kubectl apply -f "$HERE/namespaces.yaml"
kubectl apply -f "$HERE/runtimeclasses.yaml"
kubectl apply -f "$HERE/sandboxes.yaml"
# 按项目定制的运行环境的构建（D-17）：只在 Kata 里。没有 Kata 时也建命名空间（构建会被准入策略拒绝，环境退回 universal）。
kubectl apply -f "$HERE/builds.yaml"
kubectl apply -f "$HERE/workspaces.yaml"
kubectl apply -f "$HERE/storage.yaml"

# --- 6. 运维组件 ---
log "cert-manager $CERT_MANAGER_VERSION"
helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager --version "$CERT_MANAGER_VERSION" \
  --namespace cert-manager --create-namespace --set crds.enabled=true --wait
log "KEDA $KEDA_VERSION"
helm repo add kedacore https://kedacore.github.io/charts >/dev/null
helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null
helm repo update >/dev/null
helm upgrade --install keda kedacore/keda --version "$KEDA_VERSION" --namespace keda --create-namespace --wait
log "CloudNativePG（Chart $CNPG_CHART_VERSION）与 Barman Cloud 插件 $CNPG_BARMAN_PLUGIN_VERSION"
helm upgrade --install cnpg cnpg/cloudnative-pg --version "$CNPG_CHART_VERSION" --namespace cnpg-system --create-namespace --wait
curl -fsSL -o "$WORK/barman-cloud.yaml" \
  "https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/${CNPG_BARMAN_PLUGIN_VERSION}/manifest.yaml"
kubectl apply -f "$WORK/barman-cloud.yaml"

log "完成。下一步：deploy/cluster/verify.sh 核对沙箱运行时；然后按 README 放密钥、装数据层与 Runtime。"
