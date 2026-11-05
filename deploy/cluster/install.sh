#!/usr/bin/env bash
# 把一台新机器装成单节点 k3s 集群（docs/plan/cloud-deploy.md D-02），在机器上以 root 运行：
#   sudo deploy/cluster/install.sh
# 做的事（重复运行是安全的）：
#   1. gVisor：下载这个版本的包（runsc、containerd-shim-runsc-v1 与 gvisor-bin/），核对 sha512 后解到 /usr/local/bin。
#   2. k3s：配置（config.yaml、psa.yaml、containerd 模板、Traefik 配置）放好后按 versions.conf 的版本安装。
#   3. Kata（机器有 /dev/kvm 时）：kata-deploy 的 Helm Chart；没有 KVM 时跳过并提示，kata RuntimeClass 的 Pod 会起不来。
#   3b. Firecracker 与 jailer（有 /dev/kvm 时，核对 versions.conf 的 sha256）；给了 FC_MANAGER_BIN、FC_MANAGER_SHA256 时
#       再装 fc-manager 与它的 systemd 单元（D-20，fc-manager.service）；给了 FC_AGENT_BIN 时转换客户机的 rootfs（D-21）。
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
  # kata-deploy 4.2 起把 handler 写进 config-v3.toml.d/kata-deploy.toml（containerd 的 imports），不再改模板。
  awk '/^[[:space:]]*\[/{s=($0 ~ /kata-clh-runtime-rs/)} s && /privileged_without_host_devices[[:space:]]*=[[:space:]]*true/{f=1} END{exit !f}' \
      /var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.tmpl /var/lib/rancher/k3s/agent/etc/containerd/config.toml \
      /var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/*.toml 2>/dev/null \
    || die "Kata 的 handler 没有 privileged_without_host_devices = true：构建环境的特权会碰到宿主的设备，见 README 的“项目环境的构建”"
  # 构建环境的 BuildKit 在 VM 里要读写扩展属性（overlay 的 trusted.*、文件的 security.capability），virtio-fs 默认不支持
  # （DEP-TB29：Terminal-Bench 的镜像导出时 failed to get xattr）。打开 --xattr，并把 VM 里写的一律映射到宿主的 user.virtiofs. 前缀下、
  # 宿主原有的对 VM 隐藏：宿主上不会因此多出 security、trusted 一类的属性。kata-deploy 重装会覆盖这个文件，所以每次装完都补一遍。
  kata_clh=/opt/kata/share/defaults/kata-containers/runtime-rs/runtimes/clh-runtime-rs/configuration-clh-runtime-rs.toml
  if ! grep -q -- '--xattrmap=:map::user.virtiofs.:' "$kata_clh"; then
    sed -i -E 's#^(virtio_fs_extra_args = \[.*)\]$#\1, "--xattr", "--xattrmap=:map::user.virtiofs.:"]#' "$kata_clh"
  fi
  grep -q -- '"--xattr", "--xattrmap=:map::user.virtiofs.:"' "$kata_clh" \
    || die "没能给 Kata 的 virtiofsd 加上 --xattr（$kata_clh），项目环境的构建会在导出时失败"
else
  log "没有 /dev/kvm：跳过 Kata（kata RuntimeClass 的沙箱起不来，租户策略别指到 kata）"
fi

# --- 3b. Firecracker 与 fc-manager（D-20，docs/plan/firecracker-backend.md）---
# 只在有 /dev/kvm 时装。fc-manager 是宿主上的 systemd 服务（不在 Pod 里），二进制由 services/rs-fc-manager 静态编译
# （x86_64-unknown-linux-musl），给了 FC_MANAGER_BIN 与 FC_MANAGER_SHA256 才装，按哈希核对后再放进 /usr/local/bin。
if [[ -e /dev/kvm ]]; then
  [[ -r /dev/kvm && -w /dev/kvm ]] || die "/dev/kvm 存在但读写不了（Firecracker 要它）"
  if ! command -v firecracker >/dev/null || [[ "$(firecracker --version 2>/dev/null | head -1)" != *"${FIRECRACKER_VERSION#v}"* ]]; then
    log "安装 Firecracker $FIRECRACKER_VERSION"
    fc_tgz="firecracker-${FIRECRACKER_VERSION}-x86_64.tgz"
    curl -fsSL -o "$WORK/$fc_tgz" \
      "https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}/$fc_tgz"
    echo "${FIRECRACKER_SHA256}  $WORK/$fc_tgz" | sha256sum -c - >/dev/null || die "$fc_tgz 的 sha256 与 versions.conf 不一致"
    tar -xzf "$WORK/$fc_tgz" -C "$WORK"
    fc_dir="$WORK/release-${FIRECRACKER_VERSION}-x86_64"
    install -m 0755 "$fc_dir/firecracker-${FIRECRACKER_VERSION}-x86_64" /usr/local/bin/firecracker
    install -m 0755 "$fc_dir/jailer-${FIRECRACKER_VERSION}-x86_64" /usr/local/bin/jailer
  fi
  if [[ -n "${FC_MANAGER_BIN:-}" ]]; then
    [[ -n "${FC_MANAGER_SHA256:-}" ]] || die "给了 FC_MANAGER_BIN 就要给 FC_MANAGER_SHA256"
    echo "${FC_MANAGER_SHA256}  ${FC_MANAGER_BIN}" | sha256sum -c - >/dev/null || die "FC_MANAGER_BIN 的 sha256 不对"
    log "安装 fc-manager"
    install -m 0755 "$FC_MANAGER_BIN" /usr/local/bin/fc-manager
    install -d -m 0700 /etc/agentruntime /var/lib/agentruntime/fc /var/lib/agentruntime/fc/state \
      /var/lib/agentruntime/fc/jail /var/lib/agentruntime/fc/images
    # 配置只在没有时拷样例；SecretStore 文件（sandbox/fc-manager-token）要人放，见 fc-manager.env.example。
    [[ -f /etc/agentruntime/fc-manager.env ]] || install -m 0600 "$HERE/fc-manager.env.example" /etc/agentruntime/fc-manager.env
    install -m 0644 "$HERE/fc-manager.service" /etc/systemd/system/fc-manager.service
    systemctl daemon-reload
    # 客户机的 rootfs（D-21）：给了 FC_AGENT_BIN（Rust 版 sandbox-agent 的静态二进制）就按 FC_ROOTFS（runtime=镜像，空格分隔；
    # 镜像要已经在本机 Docker 里）转换。同一个镜像与 agent 第二次直接命中缓存。
    if [[ -n "${FC_AGENT_BIN:-}" ]]; then
      for pair in ${FC_ROOTFS:-python=ghcr.io/agentruntime/sandbox-python:0.1.0 universal=ghcr.io/agentruntime/sandbox-universal:0.1.0 node=ghcr.io/agentruntime/sandbox-node:0.1.0 go=ghcr.io/agentruntime/sandbox-go:0.1.0}; do
        bash "$HERE/../images/firecracker/rootfs.sh" "${pair%%=*}" "${pair#*=}" "$FC_AGENT_BIN"
      done
    fi
    if [[ -f /etc/agentruntime/fc-manager-secrets.json && -f /var/lib/agentruntime/fc/images/vmlinux ]]; then
      ( set -a && source /etc/agentruntime/fc-manager.env && /usr/local/bin/fc-manager check ) || die "fc-manager check 没通过"
      systemctl enable --now fc-manager
    else
      log "fc-manager 装好了但没启动：先放 /etc/agentruntime/fc-manager-secrets.json 与客户机内核（deploy/images/firecracker），再 systemctl enable --now fc-manager"
    fi
  else
    log "没给 FC_MANAGER_BIN：不装 fc-manager（Worker 登记里的 firecracker 后端保持 enabled: false）"
  fi
else
  log "没有 /dev/kvm：跳过 Firecracker 与 fc-manager（firecracker 后端在 Worker 的登记里保持 enabled: false）"
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
