#!/usr/bin/env bash
# E2B 节点的防火墙（nftables；e2b 分支，docs/plan/e2b-backend.md 第 2 节）。在 E2B 节点上以 root 跑，每次开机后跑一次
# （或装成 systemd 单元）。只管从网卡进来的流量，不碰本机回环与沙箱的网络命名空间（E2B 自己的出网代理在 5010、5016～5018，
# 流量来自沙箱的 veth，不在这里拦）。
#
#   ALLOW_CIDRS      能用 E2B 的来源，逗号分隔：集群各节点的地址（Pod 出集群时伪装成节点地址）与 Pod 网段。必填。
#   EXTERNAL_IFACES  从外面进来的网卡，逗号分隔，例如 eth0,cni0,flannel.1（别的节点与本机 Pod 的流量都算）。必填。
#
# 规则：3000（api）、3002（client-proxy）只放行 ALLOW_CIDRS；3001、3003、3010、5007、5008、5009、5010、5016、5017、5018、
# 5109 一律不从这些网卡进来（5008 是 E2B 节点进程没有认证的控制口，谁连上谁就有整个 E2B）。k3s 自己的端口（6443、10250、
# flannel 的 8472/udp）不在这里，照 k3s 的要求放行。
set -euo pipefail

ALLOW_CIDRS="${ALLOW_CIDRS:?要给 ALLOW_CIDRS，例如 10.0.0.0/24,10.42.0.0/16}"
EXTERNAL_IFACES="${EXTERNAL_IFACES:?要给 EXTERNAL_IFACES，例如 eth0,cni0,flannel.1}"
TABLE="agentruntime_e2b"
PUBLIC_PORTS="3000, 3002"
LOCAL_ONLY_PORTS="3001, 3003, 3010, 5007, 5008, 5009, 5010, 5016, 5017, 5018, 5109"

command -v nft >/dev/null || { echo "要有 nft（apt-get install nftables）" >&2; exit 1; }
[[ "$ALLOW_CIDRS" =~ ^[0-9./,]+$ ]] || { echo "ALLOW_CIDRS 只能是 IPv4 地址或网段，逗号分隔" >&2; exit 1; }
[[ "$EXTERNAL_IFACES" =~ ^[A-Za-z0-9._,-]+$ ]] || { echo "EXTERNAL_IFACES 只能是网卡名，逗号分隔" >&2; exit 1; }

cidrs="${ALLOW_CIDRS//,/, }"
ifaces=""
IFS=',' read -ra parts <<<"$EXTERNAL_IFACES"
for i in "${parts[@]}"; do
  ifaces+="${ifaces:+, }\"$i\""
done

rules="$(mktemp)"
trap 'rm -f "$rules"' EXIT
cat >"$rules" <<EOF
table inet $TABLE
delete table inet $TABLE
table inet $TABLE {
  set clients {
    type ipv4_addr
    flags interval
    elements = { $cidrs }
  }
  chain input {
    type filter hook input priority -10; policy accept;
    iifname { $ifaces } tcp dport { $LOCAL_ONLY_PORTS } counter drop
    iifname { $ifaces } tcp dport { $PUBLIC_PORTS } ip saddr @clients accept
    iifname { $ifaces } tcp dport { $PUBLIC_PORTS } counter drop
  }
}
EOF
nft -c -f "$rules"
nft -f "$rules"
echo "E2B 防火墙已生效：$PUBLIC_PORTS 只给 $ALLOW_CIDRS；$LOCAL_ONLY_PORTS 不从 $EXTERNAL_IFACES 进来"
