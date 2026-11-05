#!/usr/bin/env bash
# D-21：把平台的运行环境镜像（deploy/images/images.json 里的 sandbox-*，D-14）转成 Firecracker 客户机的只读 rootfs（ext4），
# 注入 Rust 版 sandbox-agent（客户机里的 PID 1，services/rs-sandbox-agent 的 --init），按内容寻址缓存。
#
#   rootfs.sh <runtime> <镜像引用> <sandbox-agent 静态二进制> [images 目录，默认 /var/lib/agentruntime/fc/images]
#
# 例：rootfs.sh universal ghcr.io/agentruntime/sandbox-universal:0.1.0 ./sandbox-agent
#
# 做法与取舍（docs/plan/firecracker-backend.md D-21）：
#   - 只转平台自己构建的镜像（镜像在本机 Docker 里，按 cloud-deploy.md 的做法在服务器上构建）；租户的镜像（D-17）不进，
#     firecracker 后端遇到按项目定制的环境退回 universal（fcsandbox 的 unsupported_image）。所以这里用 docker export 展平
#     镜像（白名单文件已经由 Docker 处理），以 root 解进一个临时目录，再 mkfs.ext4 -d 打成盘，不自己解 OCI 的层。
#   - 缓存键：镜像 ID（配置的 sha256）＋ agent 的 sha256 ＋ 本脚本的版本（FORMAT）。同一个键第二次直接复用，不再转换；
#     复用前核对盘的内容哈希（<键>.ext4.content-sha256），对不上就删掉重转。
#   - 产物放 <images>/cache/<键>.ext4，再原子地把 <images>/<runtime>.ext4 换成它的硬链接（fc-manager 把 rootfs 硬链接进
#     每台 VM 的 jail，所以要同一个文件系统；正在跑的 VM 拿着旧的 inode，不受影响）。<runtime>.ext4.sha256 写镜像 ID，
#     fc-manager 当镜像 digest 报给环境表（D-14）。
#   - 淘汰：cache 里链接数为 1 的（既不是哪个 runtime 的当前盘、也没有 VM 在用）删掉。
#   - 客户机里要有的挂载点：/proc /sys /dev /run /tmp /workspace /mnt/scratch（agent 的 --init 挂上去）；镜像的 Env 写进
#     /etc/agentruntime/env，--init 交给 agent 与命令（K8s 后端里它们是容器的环境变量）。
set -euo pipefail

FORMAT=1
runtime="${1:?runtime}"
image="${2:?镜像引用}"
agent="${3:?sandbox-agent 二进制}"
images="${4:-/var/lib/agentruntime/fc/images}"

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
log() { printf '==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "要以 root 运行（解出来的文件要保留属主）"
[[ "$runtime" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || die "runtime 只能是小写字母、数字、- 与 _"
[[ -x "$agent" ]] || die "$agent 不是可执行文件"
command -v docker >/dev/null || die "要有 docker"
command -v mkfs.ext4 >/dev/null || die "要有 mkfs.ext4"
file "$agent" 2>/dev/null | grep -Eq 'statically linked|static-pie linked' || die "$agent 要是静态链接的（x86_64-unknown-linux-musl）"

image_id="$(docker image inspect --format '{{.Id}}' "$image")" || die "本机 Docker 里没有 $image"
image_hex="${image_id#sha256:}"
agent_hex="$(sha256sum "$agent" | cut -d' ' -f1)"
key="$(printf '%s\n%s\n%s\n' "$image_hex" "$agent_hex" "$FORMAT" | sha256sum | cut -c1-32)"

cache="$images/cache"
install -d -m 0700 "$images" "$cache"
disk="$cache/$key.ext4"

if [[ -f "$disk" ]]; then
  want="$(cat "$disk.content-sha256" 2>/dev/null || true)"
  got="$(sha256sum "$disk" | cut -d' ' -f1)"
  if [[ -n "$want" && "$want" == "$got" ]]; then
    log "$runtime：缓存命中（$key），不再转换"
  else
    log "$runtime：缓存里的盘校验不过，删掉重转"
    rm -f "$disk" "$disk.content-sha256"
  fi
fi

if [[ ! -f "$disk" ]]; then
  started=$(date +%s)
  stage="$(mktemp -d "$cache/.stage-XXXXXX")"
  tmpdisk="$cache/.$key.ext4.tmp"
  cid=""
  cleanup() { [[ -n "$cid" ]] && docker rm -f "$cid" >/dev/null 2>&1; rm -rf "$stage" "$tmpdisk"; }
  trap cleanup EXIT
  log "$runtime：展平 $image（$image_hex）"
  cid="$(docker create --network none "$image" /bin/true)"
  docker export "$cid" | tar -x --numeric-owner -C "$stage"
  rm -f "$stage/.dockerenv"
  for d in proc sys dev run tmp workspace mnt/scratch etc/agentruntime usr/local/bin; do
    install -d -m 0755 "$stage/$d"
  done
  chmod 1777 "$stage/tmp"
  install -m 0755 -o 0 -g 0 "$agent" "$stage/usr/local/bin/sandbox-agent"
  docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image" | grep -v '^$' | grep -v '^AGENT_' \
    > "$stage/etc/agentruntime/env" || true
  printf 'sandbox\n' > "$stage/etc/hostname"
  printf '127.0.0.1\tlocalhost sandbox\n::1\tlocalhost\n' > "$stage/etc/hosts"
  : > "$stage/etc/resolv.conf"
  # 大小：内容加两成、再加 256 MiB 的余量（ext4 的元数据与 inode 表）。
  used_kib="$(du -sk --apparent-size "$stage" | cut -f1)"
  size_mib=$(( used_kib * 12 / 10 / 1024 + 256 ))
  truncate -s "${size_mib}M" "$tmpdisk"
  mkfs.ext4 -q -F -L "ar-$runtime" -O ^has_journal -E root_owner=0:0 -d "$stage" "$tmpdisk"
  sha256sum "$tmpdisk" | cut -d' ' -f1 > "$tmpdisk.content-sha256"
  mv -f "$tmpdisk.content-sha256" "$disk.content-sha256"
  mv -f "$tmpdisk" "$disk"
  chmod 0644 "$disk"
  log "$runtime：转好了（${size_mib} MiB，$(( $(date +%s) - started )) 秒）"
fi

ln -f "$disk" "$images/.$runtime.ext4.new"
mv -f "$images/.$runtime.ext4.new" "$images/$runtime.ext4"
printf '%s\n' "$image_hex" > "$images/.$runtime.ext4.sha256.new"
mv -f "$images/.$runtime.ext4.sha256.new" "$images/$runtime.ext4.sha256"

# 淘汰：没有任何 runtime 指着、也没有 VM 的 jail 链着的盘。
for f in "$cache"/*.ext4; do
  [[ -e "$f" ]] || continue
  if [[ "$(stat -c %h "$f")" == 1 ]]; then
    log "删掉不再用的 $(basename "$f")"
    rm -f "$f" "$f.content-sha256"
  fi
done
log "$runtime → $(basename "$disk")（镜像 $image_hex）"
