// Package proxy 是出网网关的 HTTP 代理（设计书改动稿 W8；docs/plan/cloud-deploy.md D-16）。
//
// 沙箱里的命令经 HTTP_PROXY、HTTPS_PROXY 用它（凭据是代理认证的口令，Proxy-Authorization: Basic）：
//
//   - HTTPS 走 CONNECT：先看 CONNECT 的主机与端口，再读客户端的 TLS ClientHello，SNI 也要在授权里（没有 SNI 拒绝）；
//     不是 TLS 的流（例如授权了 22 端口的 git）只看 CONNECT 的主机。之后原样转发，不解密。
//   - HTTP 走绝对地址的请求（GET http://host/…）：看 URL 的主机（Host 头以它为准），转发时去掉 Proxy-Authorization
//     与逐跳的头；不跟随重定向（3xx 原样给客户端，客户端跟过去的那一次照样要过这里的检查）。
//   - 放行的条件：凭据有效（用途 egress，环境凭据）、环境还在、正在用环境的 Task 有覆盖这个主机与端口的有效授权、
//     主机是合法的域名而不是地址或内网名字、解析出的地址没有一个落在内网（netguard）、授权的用量与租户每天的出网字节没超。
//     连接用解析时核对过的地址（锁定），不再解析。
//   - 每个连接一条审计（业务库的 sandbox_egress_audit、日志、给了 Telemetry 时一个 runtime.egress span）：租户、Task、环境、
//     主机、端口、地址、字节数、时长、结果与原因。凭据不进日志、审计与错误信息。
//   - 开着的连接每 CheckInterval 核对一次授权：撤销、到期、Task 结束、环境销毁时断开；管理接口收回时立即断开（CloseTask）。
//
// 预览入口（Preview）是进网的方向：https://<环境号>.<预览域名>/?ar_preview=<令牌>，见 preview.go。
package proxy

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"

	"github.com/agentruntime/agent-runtime/services/go-egress-gateway/internal/grants"
	"github.com/agentruntime/agent-runtime/services/go-egress-gateway/internal/netguard"
)

// Resolver 解析域名（net.Resolver 实现了它；测试换成假的）。
type Resolver interface {
	LookupNetIP(ctx context.Context, network, host string) ([]netip.Addr, error)
}

// Dialer 建 TCP 连接（net.Dialer 实现了它；测试把公网地址映射到本机的假服务）。
type Dialer interface {
	DialContext(ctx context.Context, network, address string) (net.Conn, error)
}

// Config 是网关的依赖。
type Config struct {
	// Key 是环境凭据的密钥（SecretStore 平台作用域的 sandbox/env-credential-key）。
	Key      []byte
	Store    *grants.Store
	Resolver Resolver
	Dialer   Dialer
	// Deny 是另外拒绝的网段（集群的 Pod 与 Service 网段等）。
	Deny []netip.Prefix
	Now  func() time.Time
	Log  *slog.Logger
	// Telemetry 可选：每个连接一个 runtime.egress span（按租户）。
	Telemetry telemetry.Provider
	// CheckInterval 是开着的连接多久核对一次授权，默认 1 秒。
	CheckInterval time.Duration
	// PreviewDomain 是预览地址的域名（preview.example.com），PreviewRPS 是每个环境每秒的请求上限（默认 20）。
	PreviewDomain string
	PreviewRPS    int
	// ENV-02 的网络密钥（intercept.go）：CA 是终结 TLS 用的网关 CA（SecretStore 平台作用域的 sandbox/egress-ca-cert 与
	// sandbox/egress-ca-key）；Secrets 读租户作用域的密钥；UpstreamRoots 校验上游证书（nil 用系统的）。CA 或 Secrets 为 nil 时，
	// 配了网络密钥的主机一律拒绝（不裸发）。
	CA            *tls.Certificate
	Secrets       func(ctx context.Context, tenantID, name string) ([]byte, error)
	UpstreamRoots *x509.CertPool
}

// Gateway 是网关。
type Gateway struct {
	cfg Config

	mu   sync.Mutex
	live map[liveKey]map[*liveConn]struct{}

	// 指标
	allowed, denied, closedN atomic.Int64
	upTotal, downTotal       atomic.Int64
	active                   atomic.Int64
	reasons                  sync.Map // reason → *atomic.Int64

	previewMu      sync.Mutex
	previewBuckets map[string]*bucket

	leafMu sync.Mutex
	leaves map[string]*tls.Certificate
}

type liveKey struct{ tenant, task string }

type liveConn struct {
	once   sync.Once
	reason atomic.Value
	close  func()
}

func (c *liveConn) stop(reason string) {
	c.once.Do(func() {
		c.reason.Store(reason)
		c.close()
	})
}

func (c *liveConn) why() string {
	if v, ok := c.reason.Load().(string); ok {
		return v
	}
	return ""
}

// New 建一个网关。
func New(cfg Config) *Gateway {
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Log == nil {
		cfg.Log = slog.New(slog.NewJSONHandler(io.Discard, nil))
	}
	if cfg.CheckInterval <= 0 {
		cfg.CheckInterval = time.Second
	}
	if cfg.PreviewRPS <= 0 {
		cfg.PreviewRPS = 20
	}
	if cfg.Resolver == nil {
		cfg.Resolver = net.DefaultResolver
	}
	if cfg.Dialer == nil {
		cfg.Dialer = &net.Dialer{Timeout: 10 * time.Second}
	}
	return &Gateway{cfg: cfg, live: map[liveKey]map[*liveConn]struct{}{}, previewBuckets: map[string]*bucket{}, leaves: map[string]*tls.Certificate{}}
}

// ServeHTTP 是代理的入口。
func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodConnect:
		g.handleConnect(w, r)
	case r.URL.IsAbs():
		g.handleForward(w, r)
	case r.URL.Path == "/healthz":
		_, _ = io.WriteString(w, "ok")
	default:
		http.Error(w, "这是出网代理：用 CONNECT 或绝对地址的请求", http.StatusBadRequest)
	}
}

// --- 准入 ---

type admission struct {
	claims envcred.Claims
	sess   grants.Session
	grant  grants.Grant
	meter  *grants.Meter
	host   string
	port   int
	addr   netip.Addr
}

type refusal struct {
	status int
	reason string
}

func (g *Gateway) credential(r *http.Request) (envcred.Claims, bool) {
	auth := r.Header.Get("Proxy-Authorization")
	scheme, value, _ := strings.Cut(auth, " ")
	if !strings.EqualFold(scheme, "basic") {
		return envcred.Claims{}, false
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(value))
	if err != nil {
		return envcred.Claims{}, false
	}
	_, token, ok := strings.Cut(string(raw), ":")
	if !ok {
		return envcred.Claims{}, false
	}
	claims, err := envcred.Verify(g.cfg.Key, token, envcred.PurposeEgress, g.cfg.Now())
	return claims, err == nil
}

// grantFor：会话里覆盖这个主机与端口的第一个有效授权（按 grant_id 排好序，结果确定）。
func grantFor(sess grants.Session, host string, port int, now time.Time) (grants.Grant, string) {
	if len(sess.Grants) == 0 {
		return grants.Grant{}, "no_grant"
	}
	hostOK := false
	for _, gr := range sess.Grants {
		if !gr.Active(now) || !slices.ContainsFunc(gr.Hosts, func(p string) bool { return netguard.Covers(p, host) }) {
			continue
		}
		hostOK = true
		if slices.Contains(gr.Ports, port) {
			return gr, ""
		}
	}
	if hostOK {
		return grants.Grant{}, "port_not_granted"
	}
	return grants.Grant{}, "host_not_granted"
}

func (g *Gateway) admit(ctx context.Context, claims envcred.Claims, rawHost string, port int) (*admission, *refusal) {
	host, reason := netguard.NormalizeHost(rawHost)
	if reason != "" {
		return nil, &refusal{http.StatusForbidden, reason}
	}
	if port < 1 || port > 65535 {
		return nil, &refusal{http.StatusBadRequest, "invalid_port"}
	}
	sess, err := g.cfg.Store.Lookup(ctx, claims.TenantID, claims.EnvRef)
	if err != nil {
		if errors.Is(err, grants.ErrEnvGone) || providererr.CategoryOf(err) == providererr.Denied {
			return nil, &refusal{http.StatusForbidden, "env_not_active"}
		}
		return nil, &refusal{http.StatusServiceUnavailable, "store_unavailable"}
	}
	now := g.cfg.Now()
	grant, reason := grantFor(sess, host, port, now)
	if reason != "" {
		return &admission{claims: claims, sess: sess, host: host, port: port}, &refusal{http.StatusForbidden, reason}
	}
	adm := &admission{claims: claims, sess: sess, grant: grant, host: host, port: port}
	// 工作区选用的环境配置的联网档位（ENV-02）：在授权之内再收窄。
	if why := sess.Env.Allows(host); why != "" {
		return adm, &refusal{http.StatusForbidden, why}
	}
	if limit := claims.Limits["egress_bytes"]; limit > 0 && g.cfg.Store.UsedToday(ctx, claims.TenantID) >= limit {
		return adm, &refusal{http.StatusForbidden, "daily_egress_exceeded"}
	}
	addrs, err := g.cfg.Resolver.LookupNetIP(ctx, "ip", host)
	if err != nil || len(addrs) == 0 {
		return adm, &refusal{http.StatusBadGateway, "dns_failed"}
	}
	// 解析出的任何一个地址在内网就整个拒绝（一个名字同时指向公网与内网，是重绑定的常见手法）。
	for _, a := range addrs {
		if why := netguard.Check(a, g.cfg.Deny); why != "" {
			return adm, &refusal{http.StatusForbidden, "blocked_address:" + why}
		}
	}
	adm.addr = addrs[0]
	for _, a := range addrs {
		if a.Unmap().Is4() {
			adm.addr = a.Unmap()
			break
		}
	}
	adm.meter = g.cfg.Store.Meter(claims.TenantID, grant)
	if why := adm.meter.Open(grant, now); why != "" {
		status := http.StatusForbidden
		if why == "too_many_connections" || why == "rate_limited" {
			status = http.StatusTooManyRequests
		}
		adm.meter = nil
		return adm, &refusal{status, why}
	}
	return adm, nil
}

// --- CONNECT ---

func (g *Gateway) handleConnect(w http.ResponseWriter, r *http.Request) {
	started := g.cfg.Now()
	host, portText, err := net.SplitHostPort(r.Host)
	port, perr := strconv.Atoi(portText)
	if err != nil || perr != nil {
		http.Error(w, "CONNECT 要写 host:port", http.StatusBadRequest)
		return
	}
	claims, ok := g.credential(r)
	if !ok {
		g.proxyAuthRequired(w)
		return
	}
	rec := g.record(claims, "egress", host, port, started)
	adm, ref := g.admit(r.Context(), claims, host, port)
	if ref != nil {
		g.refuse(w, rec, adm, ref)
		return
	}
	defer adm.meter.Close()
	rec.GrantID, rec.TaskID, rec.Host, rec.Address = adm.grant.ID, adm.sess.TaskID, adm.host, adm.addr.String()
	// 网络密钥（ENV-02）：只对配置的主机的 443（HTTPS）终结 TLS、加请求头；没配网关 CA 时拒绝，不裸发。
	binding := adm.sess.Env.HeaderFor(adm.host)
	if binding != nil && port == 443 {
		if g.cfg.CA == nil || g.cfg.Secrets == nil {
			g.refuse(w, rec, adm, &refusal{http.StatusForbidden, "secret_injection_unavailable"})
			return
		}
		g.handleIntercept(w, r, rec, adm, binding)
		return
	}
	upstream, err := g.cfg.Dialer.DialContext(r.Context(), "tcp", netip.AddrPortFrom(adm.addr, uint16(port)).String())
	if err != nil {
		g.refuse(w, rec, adm, &refusal{http.StatusBadGateway, "connect_failed"})
		return
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		_ = upstream.Close()
		http.Error(w, "不支持 CONNECT", http.StatusInternalServerError)
		return
	}
	client, brw, err := hj.Hijack()
	if err != nil {
		_ = upstream.Close()
		return
	}
	if _, err := io.WriteString(client, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
		_ = client.Close()
		_ = upstream.Close()
		return
	}
	g.allowed.Add(1)
	g.splice(r.Context(), rec, adm, client, bufio.NewReaderSize(brw.Reader, 16*1024+5), upstream)
}

// checkSNI：客户端发的第一段是 TLS 握手时，ClientHello 里的 SNI 也要在授权里（同一个授权覆盖它）。
// 在上行的转发里做，下行照常流动：服务端先说话的协议（SSH、SMTP 的横幅）不受影响。
func (g *Gateway) checkSNI(reader *bufio.Reader, adm *admission) string {
	first, err := reader.Peek(1)
	if err != nil {
		return "" // 客户端什么也没发就关了：之后的转发自然结束
	}
	if first[0] != 0x16 {
		return "" // 不是 TLS：只按 CONNECT 的主机
	}
	head, err := reader.Peek(5)
	if err != nil {
		return "tls_hello_unreadable"
	}
	length := int(head[3])<<8 | int(head[4])
	record, err := reader.Peek(5 + length)
	if err != nil {
		return "tls_hello_unreadable"
	}
	sni, ok := ParseSNI(record)
	if !ok || sni == "" {
		return "sni_missing"
	}
	host, reason := netguard.NormalizeHost(sni)
	if reason != "" {
		return "sni_" + reason
	}
	if !slices.ContainsFunc(adm.grant.Hosts, func(p string) bool { return netguard.Covers(p, host) }) {
		return "sni_not_granted"
	}
	if adm.sess.Env.Allows(host) != "" || adm.sess.Env.HeaderFor(host) != nil {
		// 名单之外的 SNI；或 SNI 换成了配了网络密钥的主机（那要经终结 TLS 的那条路，见 intercept.go）。
		return "sni_not_granted"
	}
	return ""
}

// splice 双向转发并计量；SNI 不在授权里、超过授权的字节上限、授权失效时断开。
func (g *Gateway) splice(ctx context.Context, rec grants.Record, adm *admission, client net.Conn, clientReader *bufio.Reader, upstream net.Conn) {
	lc := &liveConn{close: func() { _ = client.Close(); _ = upstream.Close() }}
	key := liveKey{adm.claims.TenantID, adm.sess.TaskID}
	g.track(key, lc)
	defer g.untrack(key, lc)
	g.active.Add(1)
	defer g.active.Add(-1)
	var up, down atomic.Int64
	var wg sync.WaitGroup
	wg.Add(2)
	copyDir := func(dst net.Conn, src io.Reader, upward bool) {
		defer wg.Done()
		if upward {
			if why := g.checkSNI(clientReader, adm); why != "" {
				lc.stop(why)
				return
			}
		}
		buf := make([]byte, 32*1024)
		for {
			n, err := src.Read(buf)
			if n > 0 {
				var why string
				if upward {
					why = adm.meter.Add(adm.grant, int64(n), 0)
				} else {
					why = adm.meter.Add(adm.grant, 0, int64(n))
				}
				g.cfg.Store.AddUsage(adm.claims.TenantID, int64(n))
				if why != "" {
					lc.stop(why)
					return
				}
				if _, werr := dst.Write(buf[:n]); werr != nil {
					lc.stop("")
					return
				}
				if upward {
					up.Add(int64(n))
					g.upTotal.Add(int64(n))
				} else {
					down.Add(int64(n))
					g.downTotal.Add(int64(n))
				}
			}
			if err != nil {
				lc.stop("")
				return
			}
		}
	}
	go copyDir(upstream, clientReader, true)
	go copyDir(client, upstream, false)
	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	ticker := time.NewTicker(g.cfg.CheckInterval)
	defer ticker.Stop()
	for running := true; running; {
		select {
		case <-done:
			running = false
		case <-ticker.C:
			if why := g.stillGranted(ctx, adm); why != "" {
				lc.stop(why)
			}
		}
	}
	rec.UpBytes, rec.DownBytes = up.Load(), down.Load()
	rec.Result = "allowed"
	if why := lc.why(); strings.HasPrefix(why, "sni_") || why == "tls_hello_unreadable" {
		rec.Result, rec.Reason = "denied", why
		g.denied.Add(1)
		g.count(why)
	} else if why != "" {
		rec.Result, rec.Reason = "closed", why
		g.closedN.Add(1)
		g.count(why)
	}
	g.finish(rec)
}

// stillGranted：开着的连接的授权是否还有效（缓存几秒；管理接口收回时另有 CloseTask 立即断开）。
func (g *Gateway) stillGranted(ctx context.Context, adm *admission) string {
	now := g.cfg.Now()
	if !adm.grant.Active(now) {
		return "grant_expired"
	}
	sess, err := g.cfg.Store.Lookup(ctx, adm.claims.TenantID, adm.claims.EnvRef)
	if err != nil {
		if errors.Is(err, grants.ErrEnvGone) {
			return "env_not_active"
		}
		return "" // 业务库暂时读不到：不因此断开
	}
	if sess.TaskID != adm.sess.TaskID {
		return "task_changed"
	}
	for _, gr := range sess.Grants {
		if gr.ID == adm.grant.ID && gr.Active(now) {
			return ""
		}
	}
	return "grant_revoked"
}

// --- 绝对地址的 HTTP ---

var hopHeaders = []string{"Proxy-Authorization", "Proxy-Connection", "Connection", "Keep-Alive", "Te", "Trailer", "Transfer-Encoding", "Upgrade", "Proxy-Authenticate"}

func (g *Gateway) handleForward(w http.ResponseWriter, r *http.Request) {
	started := g.cfg.Now()
	if r.URL.Scheme != "http" {
		http.Error(w, "HTTPS 要用 CONNECT", http.StatusBadRequest)
		return
	}
	port := 80
	if p := r.URL.Port(); p != "" {
		n, err := strconv.Atoi(p)
		if err != nil {
			http.Error(w, "端口不对", http.StatusBadRequest)
			return
		}
		port = n
	}
	claims, ok := g.credential(r)
	if !ok {
		g.proxyAuthRequired(w)
		return
	}
	rec := g.record(claims, "egress", r.URL.Hostname(), port, started)
	adm, ref := g.admit(r.Context(), claims, r.URL.Hostname(), port)
	if ref != nil {
		g.refuse(w, rec, adm, ref)
		return
	}
	defer adm.meter.Close()
	rec.GrantID, rec.TaskID, rec.Host, rec.Address = adm.grant.ID, adm.sess.TaskID, adm.host, adm.addr.String()
	g.allowed.Add(1)
	g.active.Add(1)
	defer g.active.Add(-1)
	target := netip.AddrPortFrom(adm.addr, uint16(port)).String()
	transport := &http.Transport{
		Proxy:                 nil,
		DialContext:           func(ctx context.Context, _, _ string) (net.Conn, error) { return g.cfg.Dialer.DialContext(ctx, "tcp", target) },
		DisableKeepAlives:     true,
		ResponseHeaderTimeout: 2 * time.Minute,
	}
	defer transport.CloseIdleConnections()
	out := r.Clone(r.Context())
	out.RequestURI = ""
	out.Host = r.URL.Host
	for _, h := range hopHeaders {
		out.Header.Del(h)
	}
	var up, down int64
	var why string
	if r.Body != nil && r.Body != http.NoBody {
		out.Body = &meteredBody{src: r.Body, add: func(n int64) string {
			up += n
			g.upTotal.Add(n)
			g.cfg.Store.AddUsage(claims.TenantID, n)
			return adm.meter.Add(adm.grant, n, 0)
		}, reason: &why}
	}
	resp, err := transport.RoundTrip(out)
	if err != nil {
		if why == "" {
			why = "upstream_failed"
		}
		rec.UpBytes, rec.Result, rec.Reason = up, "closed", why
		g.closedN.Add(1)
		g.count(why)
		g.finish(rec)
		http.Error(w, "出网网关："+why, http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	for _, h := range hopHeaders {
		resp.Header.Del(h)
	}
	for k, vs := range resp.Header {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	buf := make([]byte, 32*1024)
	checked := g.cfg.Now()
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			down += int64(n)
			g.downTotal.Add(int64(n))
			g.cfg.Store.AddUsage(claims.TenantID, int64(n))
			if why = adm.meter.Add(adm.grant, 0, int64(n)); why != "" {
				break
			}
			if _, werr := w.Write(buf[:n]); werr != nil {
				break
			}
		}
		if now := g.cfg.Now(); now.Sub(checked) >= g.cfg.CheckInterval {
			checked = now
			if why = g.stillGranted(r.Context(), adm); why != "" {
				break
			}
		}
		if rerr != nil {
			break
		}
	}
	rec.UpBytes, rec.DownBytes, rec.Result = up, down, "allowed"
	if why != "" {
		rec.Result, rec.Reason = "closed", why
		g.closedN.Add(1)
		g.count(why)
		g.finish(rec)
		panic(http.ErrAbortHandler) // 断开客户端的连接：响应已经开始写，改不了状态码
	}
	g.finish(rec)
}

type meteredBody struct {
	src    io.ReadCloser
	add    func(int64) string
	reason *string
}

func (b *meteredBody) Read(p []byte) (int, error) {
	n, err := b.src.Read(p)
	if n > 0 {
		if why := b.add(int64(n)); why != "" {
			*b.reason = why
			return 0, fmt.Errorf("出网网关：%s", why)
		}
	}
	return n, err
}

func (b *meteredBody) Close() error { return b.src.Close() }

// --- 拒绝、审计与指标 ---

func (g *Gateway) proxyAuthRequired(w http.ResponseWriter) {
	g.denied.Add(1)
	g.count("invalid_credential")
	w.Header().Set("Proxy-Authenticate", `Basic realm="agentruntime-egress"`)
	http.Error(w, "出网网关：要有效的环境凭据", http.StatusProxyAuthRequired)
}

func (g *Gateway) refuse(w http.ResponseWriter, rec grants.Record, adm *admission, ref *refusal) {
	if adm != nil {
		rec.TaskID, rec.GrantID = adm.sess.TaskID, adm.grant.ID
		if adm.host != "" {
			rec.Host = adm.host
		}
	}
	rec.Result, rec.Reason = "denied", ref.reason
	g.denied.Add(1)
	g.count(ref.reason)
	g.finish(rec)
	http.Error(w, "出网网关拒绝："+ref.reason, ref.status)
}

func newID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return "eg-" + hex.EncodeToString(b)
}

func (g *Gateway) record(claims envcred.Claims, direction, host string, port int, started time.Time) grants.Record {
	if len(host) > 253 {
		host = host[:253]
	}
	return grants.Record{ID: newID(), Tenant: claims.TenantID, EnvRef: claims.EnvRef, Direction: direction, Host: strings.ToLower(host), Port: port, Started: started}
}

// finish 记审计：业务库一行、日志一行、给了 Telemetry 时一个 runtime.egress span。
func (g *Gateway) finish(rec grants.Record) {
	rec.Duration = g.cfg.Now().Sub(rec.Started)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := g.cfg.Store.Audit(ctx, rec); err != nil {
		g.cfg.Log.Warn("审计写不进业务库", "env", rec.EnvRef, "err", err.Error())
	}
	g.cfg.Log.Info("出网", "tenant", rec.Tenant, "task", rec.TaskID, "env", rec.EnvRef, "grant", rec.GrantID, "direction", rec.Direction,
		"host", rec.Host, "port", rec.Port, "address", rec.Address, "up", rec.UpBytes, "down", rec.DownBytes, "ms", rec.Duration.Milliseconds(),
		"result", rec.Result, "reason", rec.Reason)
	if g.cfg.Telemetry == nil || rec.Tenant == "" {
		return
	}
	var tel telemetry.Telemetry
	var err error
	if rec.Tenant == "_platform" {
		tel, err = g.cfg.Telemetry.BindPlatform("egress-gateway")
	} else {
		tel, err = g.cfg.Telemetry.Bind(&commonv1.TenantContext{TenantId: rec.Tenant})
	}
	if err != nil {
		return
	}
	_, span, err := tel.StartSpan(ctx, "runtime.egress",
		telemetry.Attribute{Key: "sandbox.env_ref", Value: rec.EnvRef},
		telemetry.Attribute{Key: "task.id", Value: rec.TaskID},
		telemetry.Attribute{Key: "egress.grant_id", Value: rec.GrantID},
		telemetry.Attribute{Key: "egress.direction", Value: rec.Direction},
		telemetry.Attribute{Key: "egress.host", Value: rec.Host},
		telemetry.Attribute{Key: "egress.port", Value: int64(rec.Port)},
		telemetry.Attribute{Key: "egress.address", Value: rec.Address},
		telemetry.Attribute{Key: "egress.up_bytes", Value: rec.UpBytes},
		telemetry.Attribute{Key: "egress.down_bytes", Value: rec.DownBytes},
		telemetry.Attribute{Key: "egress.duration_ms", Value: rec.Duration.Milliseconds()},
		telemetry.Attribute{Key: "egress.result", Value: rec.Result},
		telemetry.Attribute{Key: "egress.reason", Value: rec.Reason},
	)
	if err == nil {
		span.End()
	}
}

func (g *Gateway) count(reason string) {
	if reason == "" {
		return
	}
	if i := strings.IndexByte(reason, ':'); i > 0 {
		reason = reason[:i]
	}
	v, _ := g.reasons.LoadOrStore(reason, new(atomic.Int64))
	v.(*atomic.Int64).Add(1)
}

func (g *Gateway) track(k liveKey, c *liveConn) {
	g.mu.Lock()
	defer g.mu.Unlock()
	m := g.live[k]
	if m == nil {
		m = map[*liveConn]struct{}{}
		g.live[k] = m
	}
	m[c] = struct{}{}
}

func (g *Gateway) untrack(k liveKey, c *liveConn) {
	g.mu.Lock()
	defer g.mu.Unlock()
	delete(g.live[k], c)
	if len(g.live[k]) == 0 {
		delete(g.live, k)
	}
}

// CloseTask 立即断开一个 Task 开着的全部连接（管理接口收回授权之后）。
func (g *Gateway) CloseTask(tenant, task, reason string) int {
	g.mu.Lock()
	conns := make([]*liveConn, 0, len(g.live[liveKey{tenant, task}]))
	for c := range g.live[liveKey{tenant, task}] {
		conns = append(conns, c)
	}
	g.mu.Unlock()
	for _, c := range conns {
		c.stop(reason)
	}
	return len(conns)
}

// Metrics 是 Prometheus 文本格式的指标。
func (g *Gateway) Metrics(w io.Writer) {
	fmt.Fprintf(w, "# TYPE agentruntime_egress_connections_total counter\n")
	fmt.Fprintf(w, "agentruntime_egress_connections_total{result=\"allowed\"} %d\n", g.allowed.Load())
	fmt.Fprintf(w, "agentruntime_egress_connections_total{result=\"denied\"} %d\n", g.denied.Load())
	fmt.Fprintf(w, "agentruntime_egress_connections_total{result=\"closed\"} %d\n", g.closedN.Load())
	fmt.Fprintf(w, "# TYPE agentruntime_egress_bytes_total counter\n")
	fmt.Fprintf(w, "agentruntime_egress_bytes_total{direction=\"up\"} %d\n", g.upTotal.Load())
	fmt.Fprintf(w, "agentruntime_egress_bytes_total{direction=\"down\"} %d\n", g.downTotal.Load())
	fmt.Fprintf(w, "# TYPE agentruntime_egress_connections_active gauge\nagentruntime_egress_connections_active %d\n", g.active.Load())
	fmt.Fprintf(w, "# TYPE agentruntime_egress_refusals_total counter\n")
	var reasons []string
	g.reasons.Range(func(k, _ any) bool { reasons = append(reasons, k.(string)); return true })
	slices.Sort(reasons)
	for _, k := range reasons {
		v, _ := g.reasons.Load(k)
		fmt.Fprintf(w, "agentruntime_egress_refusals_total{reason=%q} %d\n", k, v.(*atomic.Int64).Load())
	}
}

// --- TLS ---

// ParseSNI 从一条 TLS 记录（含 5 字节的头）里的 ClientHello 取出 server_name；不是 ClientHello 或格式不对时 ok 为 false。
func ParseSNI(record []byte) (string, bool) {
	if len(record) < 5 || record[0] != 0x16 {
		return "", false
	}
	body := record[5:]
	if len(body) < 4 || body[0] != 0x01 {
		return "", false
	}
	n := int(body[1])<<16 | int(body[2])<<8 | int(body[3])
	if len(body) < 4+n {
		return "", false
	}
	p := body[4 : 4+n]
	skip := func(k int) bool {
		if len(p) < k {
			return false
		}
		p = p[k:]
		return true
	}
	vec := func(lenBytes int) ([]byte, bool) {
		if len(p) < lenBytes {
			return nil, false
		}
		l := 0
		for i := 0; i < lenBytes; i++ {
			l = l<<8 | int(p[i])
		}
		if len(p) < lenBytes+l {
			return nil, false
		}
		v := p[lenBytes : lenBytes+l]
		p = p[lenBytes+l:]
		return v, true
	}
	if !skip(2 + 32) { // 版本与随机数
		return "", false
	}
	for _, lb := range []int{1, 2, 1} { // session id、密码套件、压缩方法
		if _, ok := vec(lb); !ok {
			return "", false
		}
	}
	if len(p) == 0 {
		return "", true // 没有扩展
	}
	exts, ok := vec(2)
	if !ok {
		return "", false
	}
	for len(exts) >= 4 {
		typ := int(exts[0])<<8 | int(exts[1])
		l := int(exts[2])<<8 | int(exts[3])
		if len(exts) < 4+l {
			return "", false
		}
		data := exts[4 : 4+l]
		exts = exts[4+l:]
		if typ != 0 {
			continue
		}
		if len(data) < 2 {
			return "", false
		}
		list := data[2:]
		for len(list) >= 3 {
			nameType := list[0]
			nl := int(list[1])<<8 | int(list[2])
			if len(list) < 3+nl {
				return "", false
			}
			if nameType == 0 {
				return string(list[3 : 3+nl]), true
			}
			list = list[3+nl:]
		}
		return "", true
	}
	return "", true
}

// bucket 是简单的令牌桶。
type bucket struct {
	tokens float64
	last   time.Time
}

func (g *Gateway) allowPreview(env string) bool {
	g.previewMu.Lock()
	defer g.previewMu.Unlock()
	now := g.cfg.Now()
	b := g.previewBuckets[env]
	rate := float64(g.cfg.PreviewRPS)
	if b == nil {
		b = &bucket{tokens: rate, last: now}
		g.previewBuckets[env] = b
	}
	b.tokens = min(rate, b.tokens+now.Sub(b.last).Seconds()*rate)
	b.last = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}
