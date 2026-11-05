// Package netguard 是 SSRF 防线（与 services/go-egress-gateway/internal/netguard 同一套规则；task-service 的 MCP 连接用这一份）（设计书改动稿 W8；docs/plan/cloud-deploy.md D-16 的“难点与防错”）：
//
//   - 目标只能是域名：IP 地址的任何写法（点分、十进制整数、八进制、十六进制、简写的 127.1、IPv6、方括号、IPv4 映射）
//     都当作地址拒绝，不去解析（LiteralIP）。本机与集群内的名字（localhost、.local、.internal、.svc 等）也拒绝。
//   - 域名由网关自己解析，解析出的任何一个地址落在私有网段、回环、链路本地（含云元数据 169.254.169.254）、CGNAT、
//     唯一本地（含 fd00:ec2::254）、组播、保留与文档网段，或部署另配的网段（集群的 Pod 与 Service 网段）里，就整个拒绝；
//     IPv4 映射、NAT64 的地址按里面的 IPv4 判断，6to4 与 Teredo 一律拒绝（Check）。
//   - 连接时用解析出、核对过的那个地址（锁定），不再解析，DNS 重绑定无从下手。这一步在 proxy 包里做。
package netguard

import (
	"net/netip"
	"strconv"
	"strings"
)

func prefixes(list ...string) []netip.Prefix {
	out := make([]netip.Prefix, 0, len(list))
	for _, s := range list {
		out = append(out, netip.MustParsePrefix(s))
	}
	return out
}

var (
	metadata = map[netip.Addr]bool{
		netip.MustParseAddr("169.254.169.254"): true, // AWS、GCP、Azure、OpenStack 等
		netip.MustParseAddr("169.254.170.2"):   true, // ECS 任务元数据
		netip.MustParseAddr("100.100.100.200"): true, // 阿里云
		netip.MustParseAddr("fd00:ec2::254"):   true, // AWS 的 IPv6 元数据
	}
	blocked = []struct {
		reason string
		nets   []netip.Prefix
	}{
		{"loopback", prefixes("127.0.0.0/8", "::1/128")},
		{"unspecified", prefixes("0.0.0.0/8", "::/128")},
		{"private", prefixes("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")},
		{"cgnat", prefixes("100.64.0.0/10")},
		{"link_local", prefixes("169.254.0.0/16", "fe80::/10")},
		{"unique_local", prefixes("fc00::/7", "fec0::/10")},
		{"multicast", prefixes("224.0.0.0/4", "ff00::/8")},
		{"reserved", prefixes("240.0.0.0/4", "192.0.0.0/24", "192.88.99.0/24", "198.18.0.0/15", "100::/64", "64:ff9b:1::/48")},
		{"documentation", prefixes("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")},
		{"tunnel", prefixes("2001::/32", "2002::/16")},
		{"ipv4_compatible", prefixes("::/96")},
	}
	nat64 = netip.MustParsePrefix("64:ff9b::/96")
)

// Check 返回不能连这个地址的原因；能连时为空。extra 是部署另配的拒绝网段（集群的 Pod、Service 网段等）。
func Check(addr netip.Addr, extra []netip.Prefix) string {
	if !addr.IsValid() {
		return "invalid"
	}
	addr = addr.WithZone("")
	if addr.Is4In6() {
		addr = addr.Unmap()
	}
	if metadata[addr] {
		return "cloud_metadata"
	}
	if addr.Is6() && nat64.Contains(addr) {
		b := addr.As16()
		if reason := Check(netip.AddrFrom4([4]byte{b[12], b[13], b[14], b[15]}), extra); reason != "" {
			return "nat64_" + reason
		}
		return ""
	}
	for _, p := range extra {
		if p.Contains(addr) {
			return "cluster"
		}
	}
	for _, b := range blocked {
		for _, p := range b.nets {
			if p.Contains(addr) {
				return b.reason
			}
		}
	}
	if !addr.IsGlobalUnicast() {
		return "not_global"
	}
	return ""
}

// ParseCIDRs 解析逗号分隔的网段（AR_EGRESS_DENY_CIDRS）。
func ParseCIDRs(s string) ([]netip.Prefix, error) {
	var out []netip.Prefix
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		p, err := netip.ParsePrefix(part)
		if err != nil {
			return nil, err
		}
		out = append(out, p.Masked())
	}
	return out, nil
}

// LiteralIP 判断 host 是不是 IP 地址的某种写法；是的话给出它表示的地址。inet_aton 认的写法都算：
// 1 到 4 段，每段十进制、0 开头的八进制或 0x 开头的十六进制，最后一段填满剩下的字节（127.1、2130706433、0x7f.1、0177.0.0.1）。
func LiteralIP(host string) (netip.Addr, bool) {
	h := strings.TrimSuffix(strings.TrimSpace(host), ".")
	if strings.HasPrefix(h, "[") && strings.HasSuffix(h, "]") {
		h = h[1 : len(h)-1]
	}
	if a, err := netip.ParseAddr(h); err == nil {
		return a, true
	}
	if i := strings.IndexByte(h, '%'); i >= 0 { // 带区域的 IPv6
		if a, err := netip.ParseAddr(h[:i]); err == nil {
			return a, true
		}
	}
	parts := strings.Split(h, ".")
	if len(parts) == 0 || len(parts) > 4 {
		return netip.Addr{}, false
	}
	vals := make([]uint64, len(parts))
	for i, p := range parts {
		v, ok := parseNumber(p)
		if !ok {
			return netip.Addr{}, false
		}
		vals[i] = v
	}
	var n uint64
	last := vals[len(vals)-1]
	for i, v := range vals[:len(vals)-1] {
		if v > 0xff {
			return netip.Addr{}, false
		}
		n |= v << (24 - 8*uint(i))
	}
	if last >= 1<<(8*uint(5-len(vals))) {
		return netip.Addr{}, false
	}
	n |= last
	return netip.AddrFrom4([4]byte{byte(n >> 24), byte(n >> 16), byte(n >> 8), byte(n)}), true
}

func parseNumber(s string) (uint64, bool) {
	if s == "" {
		return 0, false
	}
	base := 10
	digits := s
	switch {
	case strings.HasPrefix(s, "0x") || strings.HasPrefix(s, "0X"):
		base, digits = 16, s[2:]
		if digits == "" {
			return 0, true
		}
	case len(s) > 1 && s[0] == '0':
		base, digits = 8, s[1:]
	}
	v, err := strconv.ParseUint(digits, base, 64)
	if err != nil || v > 0xffffffff {
		return 0, false
	}
	return v, true
}

var internalSuffixes = []string{".local", ".localhost", ".internal", ".svc", ".cluster.local", ".home.arpa", ".lan", ".intranet", ".corp"}

// NormalizeHost 把目标主机规范成小写、去掉末尾的点；不是合法域名、是地址、是本机或集群内的名字时给出拒绝的原因。
func NormalizeHost(host string) (string, string) {
	h := strings.ToLower(strings.TrimSuffix(strings.TrimSpace(host), "."))
	if h == "" || len(h) > 253 {
		return "", "invalid_host"
	}
	if _, ok := LiteralIP(h); ok {
		return "", "ip_literal"
	}
	for _, label := range strings.Split(h, ".") {
		if label == "" || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
			return "", "invalid_host"
		}
		for _, c := range label {
			if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-') {
				return "", "invalid_host"
			}
		}
	}
	if h == "localhost" || !strings.Contains(h, ".") {
		return "", "internal_name"
	}
	for _, s := range internalSuffixes {
		if strings.HasSuffix(h, s) {
			return "", "internal_name"
		}
	}
	return h, ""
}

// Covers：授权里的一项（可以 *. 开头）是否覆盖具体的主机。*.example.com 覆盖 a.example.com 与 a.b.example.com，不覆盖 example.com。
func Covers(pattern, host string) bool {
	pattern = strings.ToLower(pattern)
	if pattern == host {
		return true
	}
	if !strings.HasPrefix(pattern, "*.") {
		return false
	}
	return strings.HasSuffix(host, pattern[1:]) && host != pattern[2:]
}
