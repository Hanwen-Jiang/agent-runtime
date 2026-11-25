package crawler

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"
)

const (
	ZoneExternal = "external"
	ZoneInternal = "internal"
	MaxRedirects = 5
)

var (
	ErrBlocked = errors.New("request blocked by crawler policy")
	ErrInvalid = errors.New("invalid crawler request")
)

var blockedInfraPorts = map[int]bool{
	22:   true, // SSH
	2379: true, // etcd
	5432: true, // Postgres
	6379: true, // Redis / Valkey
	6443: true, // Kubernetes API
	7233: true, // Temporal gRPC
	8123: true, // ClickHouse HTTP
	9000: true, // ClickHouse native / MinIO
	9093: true, // Redpanda Kafka
}

var alwaysBlockedHosts = map[string]bool{
	"169.254.169.254":          true,
	"metadata.google.internal": true,
	"metadata.goog":            true,
	"10.43.0.1":                true,
	"kubernetes.default":       true,
	"kubernetes.default.svc":   true,
}

var _, cgnatPrefix, _ = net.ParseCIDR("100.64.0.0/10")

// PolicyConfig governs dual-zone SSRF isolation and domain allow/deny enforcement.
type PolicyConfig struct {
	ExternalAllowDomains []string
	ExternalDenyDomains  []string
	InternalAllowHosts   []string // exact "host" or "host:port" entries
	AllowPrivateForTest  bool     // unit tests against httptest.Server only
	LookupIP             func(ctx context.Context, network, host string) ([]net.IP, error)
}

type Policy struct {
	extAllow  []string
	extDeny   []string
	intAllow  map[string]bool
	allowPriv bool
	lookupIP  func(ctx context.Context, network, host string) ([]net.IP, error)
}

func NewPolicy(cfg PolicyConfig) *Policy {
	extAllow := normalizeList(cfg.ExternalAllowDomains)
	extDeny := normalizeList(cfg.ExternalDenyDomains)
	intAllow := map[string]bool{}
	for _, entry := range cfg.InternalAllowHosts {
		clean := strings.TrimSpace(strings.ToLower(entry))
		if clean != "" {
			intAllow[clean] = true
		}
	}
	lookup := cfg.LookupIP
	if lookup == nil {
		lookup = net.DefaultResolver.LookupIP
	}
	return &Policy{
		extAllow:  extAllow,
		extDeny:   extDeny,
		intAllow:  intAllow,
		allowPriv: cfg.AllowPrivateForTest,
		lookupIP:  lookup,
	}
}

func normalizeList(items []string) []string {
	out := make([]string, 0, len(items))
	for _, item := range items {
		clean := strings.TrimPrefix(strings.TrimSpace(strings.ToLower(item)), ".")
		if clean != "" {
			out = append(out, clean)
		}
	}
	return out
}

func matchesDomain(host, pattern string) bool {
	if pattern == "*" {
		return true
	}
	return host == pattern || strings.HasSuffix(host, "."+pattern)
}

func IsPublicIP(ip net.IP) bool {
	if ip == nil {
		return false
	}
	if v4 := ip.To4(); v4 != nil {
		ip = v4
	}
	if !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsMulticast() || ip.IsUnspecified() {
		return false
	}
	if cgnatPrefix != nil && cgnatPrefix.Contains(ip) {
		return false
	}
	addr, ok := netip.AddrFromSlice(ip)
	if !ok {
		return false
	}
	addr = addr.Unmap()
	if addr.IsPrivate() || addr.IsLoopback() || addr.IsLinkLocalUnicast() || addr.IsUnspecified() {
		return false
	}
	// Block RFC1918 / benchmarking / documentation prefixes via netip checks as well.
	return addr.IsGlobalUnicast()
}

func (p *Policy) InternalAllowCount() int {
	return len(p.intAllow)
}

// ValidateTarget checks URL syntax, zone rules, deny/allow lists, and returns resolved dial address when external.
func (p *Policy) ValidateTarget(ctx context.Context, rawURL, zone string) (*url.URL, int, []net.IP, error) {
	if zone == "" {
		zone = ZoneExternal
	}
	if zone != ZoneExternal && zone != ZoneInternal {
		return nil, 0, nil, fmt.Errorf("%w: unsupported zone %q", ErrInvalid, zone)
	}
	u, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.Hostname() == "" {
		return nil, 0, nil, fmt.Errorf("%w: only http/https URLs with host are allowed", ErrInvalid)
	}
	if u.User != nil {
		return nil, 0, nil, fmt.Errorf("%w: URL credentials are prohibited", ErrBlocked)
	}
	host := strings.ToLower(strings.TrimSpace(u.Hostname()))
	port := 80
	if u.Scheme == "https" {
		port = 443
	}
	if rawPort := u.Port(); rawPort != "" {
		parsedPort, convErr := strconv.Atoi(rawPort)
		if convErr != nil || parsedPort < 1 || parsedPort > 65535 {
			return nil, 0, nil, fmt.Errorf("%w: invalid port", ErrInvalid)
		}
		port = parsedPort
	}

	if alwaysBlockedHosts[host] || strings.HasSuffix(host, ".169.254.169.254") {
		return nil, 0, nil, fmt.Errorf("%w: metadata/control-plane host %q is hard-blocked", ErrBlocked, host)
	}
	if blockedInfraPorts[port] {
		return nil, 0, nil, fmt.Errorf("%w: infrastructure port %d is blocked", ErrBlocked, port)
	}

	if zone == ZoneInternal {
		hostPort := fmt.Sprintf("%s:%d", host, port)
		if !p.intAllow[hostPort] && !p.intAllow[host] {
			return nil, 0, nil, fmt.Errorf("%w: internal target %q is not in allowlist", ErrBlocked, hostPort)
		}
		return u, port, nil, nil
	}

	// ZoneExternal: enforce domain lists + public IP check.
	for _, deny := range p.extDeny {
		if matchesDomain(host, deny) {
			return nil, 0, nil, fmt.Errorf("%w: domain %q is on deny list", ErrBlocked, host)
		}
	}
	if len(p.extAllow) > 0 {
		allowed := false
		for _, allow := range p.extAllow {
			if matchesDomain(host, allow) {
				allowed = true
				break
			}
		}
		if !allowed {
			return nil, 0, nil, fmt.Errorf("%w: domain %q is not in external allowlist", ErrBlocked, host)
		}
	}

	if ip := net.ParseIP(host); ip != nil {
		if !p.allowPriv && !IsPublicIP(ip) {
			return nil, 0, nil, fmt.Errorf("%w: IP %q resolves to non-public space", ErrBlocked, host)
		}
		return u, port, []net.IP{ip}, nil
	}

	ips, err := p.lookupIP(ctx, "ip", host)
	if err != nil || len(ips) == 0 {
		return nil, 0, nil, fmt.Errorf("dns lookup failed for %q: %v", host, err)
	}
	for _, ip := range ips {
		if !p.allowPriv && !IsPublicIP(ip) {
			return nil, 0, nil, fmt.Errorf("%w: host %q resolved to non-public IP %s", ErrBlocked, host, ip.String())
		}
	}
	return u, port, ips, nil
}

// FetchSafe performs an HTTP GET pinning resolved IPs for external fetches and re-validating every redirect hop.
func (p *Policy) FetchSafe(ctx context.Context, rawURL, zone string, maxBytes int64, followRedirects bool, timeout time.Duration) (finalURL string, status int, hdr http.Header, body []byte, truncated bool, redirects []string, err error) {
	if maxBytes <= 0 || maxBytes > 2<<20 {
		maxBytes = 1 << 20
	}
	current := strings.TrimSpace(rawURL)
	seen := []string{}
	dialer := &net.Dialer{Timeout: timeout}

	for hop := 0; hop <= MaxRedirects; hop++ {
		parsed, port, pinnedIPs, vErr := p.ValidateTarget(ctx, current, zone)
		if vErr != nil {
			if len(seen) > 0 {
				return current, 0, nil, nil, false, seen, vErr
			}
			return current, 0, nil, nil, false, nil, vErr
		}

		transport := &http.Transport{
			Proxy: nil, // Never inherit HTTP_PROXY / HTTPS_PROXY environment proxies
			DialContext: func(dctx context.Context, network, addr string) (net.Conn, error) {
				if len(pinnedIPs) > 0 && zone == ZoneExternal {
					targetAddr := net.JoinHostPort(pinnedIPs[0].String(), strconv.Itoa(port))
					return dialer.DialContext(dctx, network, targetAddr)
				}
				return dialer.DialContext(dctx, network, addr)
			},
		}
		client := &http.Client{
			Timeout:   timeout,
			Transport: transport,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return http.ErrUseLastResponse
			},
		}

		req, reqErr := http.NewRequestWithContext(ctx, http.MethodGet, parsed.String(), nil)
		if reqErr != nil {
			transport.CloseIdleConnections()
			return current, 0, nil, nil, false, seen, reqErr
		}
		req.Header.Set("User-Agent", "agentruntime-crawler/1.0")
		req.Header.Set("Accept", "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8")

		resp, doErr := client.Do(req)
		if doErr != nil {
			transport.CloseIdleConnections()
			return current, 0, nil, nil, false, seen, doErr
		}

		// Strip sensitive headers from downstream visibility.
		cleanHdr := resp.Header.Clone()
		cleanHdr.Del("Set-Cookie")

		loc := resp.Header.Get("Location")
		if resp.StatusCode >= 300 && resp.StatusCode < 400 && loc != "" && followRedirects && hop < MaxRedirects {
			resp.Body.Close()
			transport.CloseIdleConnections()
			nextURL, relErr := parsed.Parse(loc)
			if relErr != nil {
				return current, resp.StatusCode, cleanHdr, nil, false, seen, fmt.Errorf("%w: invalid redirect target", ErrBlocked)
			}
			nextStr := nextURL.String()
			if !slices.Contains(seen, nextStr) {
				seen = append(seen, nextStr)
			}
			current = nextStr
			continue
		}

		rawBody, readErr := readLimited(resp.Body, maxBytes)
		resp.Body.Close()
		transport.CloseIdleConnections()
		if readErr != nil {
			return current, resp.StatusCode, cleanHdr, nil, false, seen, readErr
		}
		trunc := int64(len(rawBody)) > maxBytes
		if trunc {
			rawBody = rawBody[:maxBytes]
		}
		return parsed.String(), resp.StatusCode, cleanHdr, rawBody, trunc, seen, nil
	}
	return current, 0, nil, nil, false, seen, fmt.Errorf("%w: exceeded max redirects (%d)", ErrBlocked, MaxRedirects)
}

func readLimited(r io.Reader, maxBytes int64) ([]byte, error) {
	buf := make([]byte, 0, min(maxBytes+1, 32*1024))
	tmp := make([]byte, 8192)
	for int64(len(buf)) <= maxBytes {
		n, err := r.Read(tmp)
		if n > 0 {
			remain := int(maxBytes+1) - len(buf)
			if n > remain {
				n = remain
			}
			buf = append(buf, tmp[:n]...)
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				return buf, nil
			}
			return nil, err
		}
	}
	return buf, nil
}
