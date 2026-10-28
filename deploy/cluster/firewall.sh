#!/usr/bin/env bash
# 公网网卡的防火墙（README 第 2 步；docs/plan/cloud-deploy.md D-02），以 root 运行，重复运行是安全的。开机由 ar-firewall.service 跑。
# - 80、443 只收 Cloudflare 的地址段：在 raw 表的 PREROUTING 里查。servicelb 把这两个端口 DNAT 给 Traefik，包走 FORWARD 而不进 INPUT，
#   所以 ufw 之类只管 INPUT 的规则拦不住它。地址段与 k3s/traefik-config.yaml 的 trustedIPs 相同，变了两处一起改。
# - NodePort（30000–32767，例如环境镜像仓库的 30500）谁都连不上。
# - 宿主上的端口（6443 API Server、10250 kubelet 等）在公网网卡上关掉，只留 22。
# 网卡名默认 eno1，用 AR_PUBLIC_IF 改。
set -euo pipefail
IF="${AR_PUBLIC_IF:-eno1}"
CF_V4="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22"

chain() { iptables -t "$1" -N "$2" 2>/dev/null || iptables -t "$1" -F "$2"; }
jump() { local t="$1" p="$2"; shift 2; iptables -t "$t" -C "$p" "$@" 2>/dev/null || iptables -t "$t" -I "$p" 1 "$@"; }

chain raw AR-EDGE
iptables -t raw -A AR-EDGE -p tcp --dport 30000:32767 -j DROP
iptables -t raw -A AR-EDGE -p udp --dport 30000:32767 -j DROP
for net in $CF_V4; do iptables -t raw -A AR-EDGE -s "$net" -j RETURN; done
iptables -t raw -A AR-EDGE -p tcp -m multiport --dports 80,443 -j DROP
jump raw PREROUTING -i "$IF" -j AR-EDGE

chain filter AR-INPUT
iptables -A AR-INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A AR-INPUT -p icmp -j ACCEPT
iptables -A AR-INPUT -p tcp --dport 22 -j ACCEPT
iptables -A AR-INPUT -j DROP
jump filter INPUT -i "$IF" -j AR-INPUT
echo "防火墙（$IF）：$(iptables -t raw -S AR-EDGE | wc -l) 条入口规则，$(iptables -S AR-INPUT | wc -l) 条 INPUT 规则"
