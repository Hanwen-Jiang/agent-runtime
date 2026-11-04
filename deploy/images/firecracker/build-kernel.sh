#!/usr/bin/env bash
# 构建 Firecracker 的客户机内核（D-20，docs/plan/firecracker-backend.md）：按 kernel.conf 固定的版本下载内核源码与
# Firecracker 的 guest 配置，都按 sha256 核对；以 guest 配置为底、叠上 agentruntime.config（无模块、串口控制台、
# 没有网卡），构建 x86_64 的 vmlinux（firecracker 直接引导未压缩的 ELF）。在 Linux 构建机或容器里以普通用户运行：
#   deploy/images/firecracker/build-kernel.sh <输出目录>
# 输出：<输出目录>/vmlinux、vmlinux.sha256 与 config（最终的 .config）。装到宿主上 fc-manager 的 AR_FC_KERNEL
# （默认 /var/lib/agentruntime/fc/images/vmlinux，要与 rootfs 在同一个文件系统上，见 deploy/cluster/fc-manager.env.example）。
# 要有：curl、sha256sum、tar、xz、make、gcc、flex、bison、bc、perl，以及 libelf、openssl 的开发头文件。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=kernel.conf
source "$HERE/kernel.conf"

log() { printf '==> %s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

OUT="${1:-}"
[[ -n "$OUT" ]] || die "用法：build-kernel.sh <输出目录>"
[[ "$(uname -s)" == "Linux" ]] || die "要在 Linux 上构建（Windows 上在容器里跑）"
for tool in curl sha256sum tar xz make gcc flex bison bc perl; do
  command -v "$tool" >/dev/null || die "要有 $tool"
done
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

tarball="linux-${KERNEL_VERSION}.tar.xz"
log "下载 linux ${KERNEL_VERSION}"
curl -fsSL -o "$WORK/$tarball" "https://cdn.kernel.org/pub/linux/kernel/v${KERNEL_VERSION%%.*}.x/$tarball"
# 先核对再解包：哈希不对的东西不碰。
echo "${KERNEL_SHA256}  $WORK/$tarball" | sha256sum -c - >/dev/null || die "linux ${KERNEL_VERSION} 的 sha256 不对"
log "下载 Firecracker ${FC_CONFIG_REF} 的 ${FC_CONFIG_NAME}"
curl -fsSL -o "$WORK/base.config" \
  "https://raw.githubusercontent.com/firecracker-microvm/firecracker/${FC_CONFIG_REF}/resources/guest_configs/${FC_CONFIG_NAME}"
echo "${FC_CONFIG_SHA256}  $WORK/base.config" | sha256sum -c - >/dev/null || die "${FC_CONFIG_NAME} 的 sha256 不对"

tar -xJf "$WORK/$tarball" -C "$WORK"
SRC="$WORK/linux-${KERNEL_VERSION}"
[[ "$(make -s -C "$SRC" kernelversion)" == "$KERNEL_VERSION" ]] || die "解出来的源码版本不是 ${KERNEL_VERSION}"

log "配置：${FC_CONFIG_NAME} + agentruntime.config"
cp "$WORK/base.config" "$SRC/.config"
(cd "$SRC" && scripts/kconfig/merge_config.sh -m .config "$HERE/agentruntime.config" >/dev/null && make -s olddefconfig)
# 叠上去的每一项都要还在最终的 .config 里。
while IFS= read -r line; do
  if [[ "$line" =~ ^CONFIG_[A-Z0-9_]+= || "$line" =~ ^#\ CONFIG_[A-Z0-9_]+\ is\ not\ set$ ]]; then
    grep -qxF -- "$line" "$SRC/.config" || die "olddefconfig 之后没有这一项：$line"
  fi
done < "$HERE/agentruntime.config"
grep -q '=m$' "$SRC/.config" && die "最终配置里还有编成模块的项"

log "构建 vmlinux（$(nproc) 路并行）"
# 固定构建信息，同一份源码与配置构建出的内核尽量一致。
export KBUILD_BUILD_USER=agentruntime KBUILD_BUILD_HOST=build KBUILD_BUILD_TIMESTAMP="$KERNEL_RELEASED"
make -s -C "$SRC" -j"$(nproc)" vmlinux

install -m 0644 "$SRC/vmlinux" "$OUT/vmlinux"
install -m 0644 "$SRC/.config" "$OUT/config"
(cd "$OUT" && sha256sum vmlinux > vmlinux.sha256)
log "完成：$OUT/vmlinux（$(cut -d' ' -f1 "$OUT/vmlinux.sha256")）"
