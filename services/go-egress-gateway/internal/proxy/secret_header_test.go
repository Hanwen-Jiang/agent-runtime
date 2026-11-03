package proxy

// ENV-02（人工验收 Q28–32；docs/plan/env-02-implementation-2026-11-03.md）：环境配置的联网档位与网络密钥。
//
//   - 网络密钥：只在向配置的 HTTPS 主机（443、TLS）的请求上，由网关加请求头（默认 Authorization: Bearer，按主机可配置
//     header 名，例如 x-api-key）；值读自 SecretStore，沙箱、模型都拿不到；不写查询参数；明文 HTTP 不加；隧道里的 Host
//     与 CONNECT 的主机不一致时拒绝；读不到密钥时拒绝，不带空值或旧值继续。
//   - 档位：deny 一律拒绝业务出网；restricted 只放行名单里的主机（子域名是否匹配按每一项的 include_subdomains）；
//     open 不另加限制。三档都在 Task 的授权之内。

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"log/slog"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
)

const fakeToken = "tok-SECRET-0123456789"

var environmentTables = []relational.Migration{{Version: 205, Name: "workspaces", Statements: []string{
	`CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')`,
}}, {Version: 207, Name: "workspace_environment", Statements: []string{
	`ALTER TABLE workspaces ADD COLUMN environment_id TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE workspaces ADD COLUMN environment TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE workspaces ADD COLUMN environment_owner TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE workspaces ADD COLUMN environment_deleted_at TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE workspaces ADD COLUMN environment_secrets TEXT NOT NULL DEFAULT ''`,
}}}

type testCA struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pem  []byte
	kpem []byte
}

func newTestCA(t *testing.T, name string) *testCA {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: name}, NotBefore: time.Now().Add(-time.Hour),
		NotAfter: time.Now().Add(24 * time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	kder, _ := x509.MarshalECPrivateKey(key)
	return &testCA{cert: cert, key: key, pem: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), kpem: pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: kder})}
}

func (ca *testCA) leaf(t *testing.T, names ...string) tls.Certificate {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: names[0]}, DNSNames: names,
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(24 * time.Hour), ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.cert, &key.PublicKey, ca.key)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}

func (ca *testCA) pool() *x509.CertPool {
	p := x509.NewCertPool()
	p.AddCert(ca.cert)
	return p
}

// seen 是上游收到的请求头。
type seen struct {
	mu   sync.Mutex
	reqs []http.Header
	urls []string
}

func (s *seen) last() (http.Header, string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.reqs) == 0 {
		return nil, ""
	}
	return s.reqs[len(s.reqs)-1], s.urls[len(s.urls)-1]
}

func (s *seen) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.reqs)
}

type secretHarness struct {
	*harness
	gatewayCA *testCA
	upstream  *seen
	logs      *bytes.Buffer
	secrets   map[string]string
	readErr   error
}

// newSecretHarness：上游是真 TLS（证书由“公网 CA”签给 api.example.com 等），网关用自己的 CA 终结配置了网络密钥的主机。
func newSecretHarness(t *testing.T, environment map[string]any) *secretHarness {
	t.Helper()
	h := newHarness(t)
	if err := h.db.Migrate(context.Background(), environmentTables); err != nil {
		t.Fatal(err)
	}
	public := newTestCA(t, "public-ca")
	upstream := &seen{}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstream.mu.Lock()
		upstream.reqs = append(upstream.reqs, r.Header.Clone())
		upstream.urls = append(upstream.urls, r.Host+r.URL.RequestURI())
		upstream.mu.Unlock()
		_, _ = io.WriteString(w, "ok from "+r.Host)
	}))
	srv.TLS = &tls.Config{Certificates: []tls.Certificate{public.leaf(t, "api.example.com", "sub.api.example.com", "x.example.com", "other.example.com")}}
	srv.StartTLS()
	t.Cleanup(srv.Close)
	h.resolver.mu.Lock()
	h.resolver.answers["sub.api.example.com"] = []netip.Addr{publicAPI}
	h.resolver.answers["x.example.com"] = []netip.Addr{publicAPI}
	h.resolver.mu.Unlock()
	h.dialer.mu.Lock()
	h.dialer.routes["93.184.216.34:443"] = srv.Listener.Addr().String()
	h.dialer.routes["93.184.216.35:443"] = srv.Listener.Addr().String()
	h.dialer.mu.Unlock()
	sh := &secretHarness{harness: h, gatewayCA: newTestCA(t, "egress-ca"), upstream: upstream, logs: &bytes.Buffer{}, secrets: map[string]string{
		"user-secrets/u-alice/example-token": fakeToken, "user-secrets/u-alice/x-key": "xk-SECRET",
	}}
	ca, err := ParseCA(sh.gatewayCA.pem, sh.gatewayCA.kpem)
	if err != nil {
		t.Fatal(err)
	}
	h.gw.cfg.CA = ca
	h.gw.cfg.UpstreamRoots = public.pool()
	h.gw.cfg.Log = slog.New(slog.NewJSONHandler(sh.logs, nil))
	h.gw.cfg.Secrets = func(_ context.Context, tenantID, name string) ([]byte, error) {
		if sh.readErr != nil {
			return nil, sh.readErr
		}
		if tenantID != tenant {
			return nil, errors.New("wrong tenant")
		}
		v, ok := sh.secrets[name]
		if !ok {
			return nil, errors.New("not found")
		}
		return []byte(v), nil
	}
	// Worker 分两列存：非机密的（network 等）与机密的（secret_headers、env）。
	nonSecret, secretPart := map[string]any{}, map[string]any{}
	for k, v := range environment {
		if k == "secret_headers" || k == "env" {
			secretPart[k] = v
		} else {
			nonSecret[k] = v
		}
	}
	doc, _ := json.Marshal(nonSecret)
	secretDoc := ""
	if len(secretPart) > 0 {
		raw, _ := json.Marshal(secretPart)
		secretDoc = string(raw)
	}
	h.exec(`INSERT INTO workspaces (workspace_id, state, environment_id, environment, environment_secrets, environment_owner) VALUES ('w1', 'active', 'env-py', $1, $2, 'u-alice')`, string(doc), secretDoc)
	h.exec(`UPDATE sandbox_envs SET workspace_id = 'w1' WHERE env_ref = $1`, envRef)
	h.grant(grantSpec{id: "g1", hosts: []string{"api.example.com", "*.api.example.com", "x.example.com", "other.example.com"}, ports: []int{80, 443}})
	return sh
}

// sandboxClient 是沙箱里的程序：经代理，信任网关的 CA（Worker 交给它的证书包），不知道任何密钥。
func (sh *secretHarness) sandboxClient(extraRoots ...*x509.Certificate) *http.Client {
	u, _ := url.Parse(sh.proxy.URL)
	u.User = url.UserPassword("ar", sh.token("egress", nil))
	roots := sh.gatewayCA.pool()
	for _, c := range extraRoots {
		roots.AddCert(c)
	}
	return &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{
		Proxy: http.ProxyURL(u), TLSClientConfig: &tls.Config{RootCAs: roots}, DisableKeepAlives: true,
	}}
}

func defaultEnvironment() map[string]any {
	return map[string]any{
		"environment_id": "env-py",
		"network":        map[string]any{"mode": "open"},
		"secret_headers": []any{
			map[string]any{"host": "api.example.com", "include_subdomains": false, "secret_name": "example-token", "header": "authorization"},
			map[string]any{"host": "x.example.com", "include_subdomains": false, "secret_name": "x-key", "header": "x-api-key"},
		},
	}
}

func (sh *secretHarness) assertNoTokenAnywhere(t *testing.T) {
	t.Helper()
	for _, a := range sh.audits() {
		if strings.Contains(strings.Join([]string{str(a["reason"]), str(a["host"])}, " "), "SECRET") {
			t.Fatalf("审计里有密钥：%v", a)
		}
	}
	if strings.Contains(sh.logs.String(), "SECRET") {
		t.Fatalf("日志里有密钥：%s", sh.logs.String())
	}
}

func str(v any) string { s, _ := v.(string); return s }

func TestSecretHeaderIsAddedOnlyForTheConfiguredHTTPSHost(t *testing.T) {
	sh := newSecretHarness(t, defaultEnvironment())
	c := sh.sandboxClient()
	req, _ := http.NewRequest(http.MethodGet, "https://api.example.com/v1/models?q=1", nil)
	req.Header.Set("Authorization", "Bearer forged-by-sandbox")
	resp, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || !strings.Contains(string(body), "ok from api.example.com") {
		t.Fatalf("经网关的请求没到上游：%d %s", resp.StatusCode, body)
	}
	headers, target := sh.upstream.last()
	if got := headers.Values("Authorization"); len(got) != 1 || got[0] != "Bearer "+fakeToken {
		t.Fatalf("上游要收到网关加的头（沙箱自己写的被替换）：%v", got)
	}
	if strings.Contains(target, fakeToken) || target != "api.example.com/v1/models?q=1" {
		t.Fatalf("不写进 URL 与查询参数：%s", target)
	}
	// 按主机配置的 header 名：值是密钥原文。
	if _, err := c.Get("https://x.example.com/ping"); err != nil {
		t.Fatal(err)
	}
	headers, _ = sh.upstream.last()
	if headers.Get("X-Api-Key") != "xk-SECRET" || headers.Get("Authorization") != "" {
		t.Fatalf("x-api-key：%v", headers)
	}
	sh.assertNoTokenAnywhere(t)
}

func TestNoSecretForOtherHostsSubdomainsOrPlainHTTP(t *testing.T) {
	sh := newSecretHarness(t, defaultEnvironment())
	// 没配网络密钥的主机：原样隧道（网关不解密）；沙箱用上游的证书校验。
	plain := sh.client(sh.token("egress", nil), true)
	if code, body := sh.get(plain, "https://other.example.com/"); code != 200 {
		t.Fatalf("别的主机照常放行：%d %s", code, body)
	}
	if h, _ := sh.upstream.last(); h.Get("Authorization") != "" {
		t.Fatal("别的主机不加头")
	}
	// include_subdomains = false：子域名不加（而且这里不终结 TLS）。
	if code, _ := sh.get(plain, "https://sub.api.example.com/"); code != 200 {
		t.Fatalf("子域名照常隧道：%d", code)
	}
	if h, _ := sh.upstream.last(); h.Get("Authorization") != "" {
		t.Fatal("子域名不加头（include_subdomains = false）")
	}
	// 明文 HTTP：不加头（只对 HTTPS）。
	if code, _ := sh.get(plain, "http://api.example.com/"); code != 200 {
		t.Fatalf("明文 HTTP 照常转发：%d", code)
	}
	sh.assertNoTokenAnywhere(t)
}

func TestSubdomainsAreIncludedOnlyWhenConfigured(t *testing.T) {
	env := defaultEnvironment()
	env["secret_headers"] = []any{map[string]any{"host": "api.example.com", "include_subdomains": true, "secret_name": "example-token", "header": "authorization"}}
	sh := newSecretHarness(t, env)
	if _, err := sh.sandboxClient().Get("https://sub.api.example.com/"); err != nil {
		t.Fatal(err)
	}
	if h, _ := sh.upstream.last(); h.Get("Authorization") != "Bearer "+fakeToken {
		t.Fatalf("include_subdomains = true 时子域名也加：%v", h)
	}
}

func TestHostMismatchInsideTheTunnelIsRefused(t *testing.T) {
	sh := newSecretHarness(t, defaultEnvironment())
	conn, status := sh.tunnel(sh.token("egress", nil), "api.example.com:443")
	defer conn.Close()
	if !strings.Contains(status, "200") {
		t.Fatalf("CONNECT：%s", status)
	}
	client := tls.Client(conn, &tls.Config{ServerName: "api.example.com", RootCAs: sh.gatewayCA.pool()})
	if err := client.Handshake(); err != nil {
		t.Fatal(err)
	}
	_, _ = io.WriteString(client, "GET / HTTP/1.1\r\nHost: other.example.com\r\n\r\n")
	resp, err := http.ReadResponse(bufio.NewReader(client), nil)
	if err == nil {
		resp.Body.Close()
		if resp.StatusCode < 400 {
			t.Fatalf("Host 与 CONNECT 不一致要拒绝：%d", resp.StatusCode)
		}
	}
	if sh.upstream.count() != 0 {
		t.Fatal("被拒的请求不到上游")
	}
}

func TestUnreadableSecretRefusesTheRequest(t *testing.T) {
	sh := newSecretHarness(t, defaultEnvironment())
	sh.readErr = errors.New("secret store unavailable")
	resp, err := sh.sandboxClient().Get("https://api.example.com/")
	if err == nil {
		resp.Body.Close()
		if resp.StatusCode < 500 {
			t.Fatalf("读不到密钥时拒绝：%d", resp.StatusCode)
		}
	}
	if sh.upstream.count() != 0 {
		t.Fatal("不带空值或旧值继续发往上游")
	}
}

func TestWithoutACASecretHostsAreRefusedNotSentBare(t *testing.T) {
	sh := newSecretHarness(t, defaultEnvironment())
	sh.gw.cfg.CA = nil
	_, status := sh.tunnel(sh.token("egress", nil), "api.example.com:443")
	if !strings.Contains(status, "403") {
		t.Fatalf("没配网关 CA 时，配了网络密钥的主机要拒绝：%s", status)
	}
	waitFor(t, "审计", func() bool {
		for _, a := range sh.audits() {
			if str(a["reason"]) == "secret_injection_unavailable" {
				return true
			}
		}
		return false
	})
}

func TestNetworkModesNarrowTheTaskGrant(t *testing.T) {
	deny := newSecretHarness(t, map[string]any{"environment_id": "env-py", "network": map[string]any{"mode": "deny"}})
	plain := deny.client(deny.token("egress", nil), true)
	if code, _ := deny.get(plain, "http://api.example.com/"); code != http.StatusForbidden {
		t.Fatalf("deny：%d", code)
	}
	if _, status := deny.tunnel(deny.token("egress", nil), "api.example.com:443"); !strings.Contains(status, "403") {
		t.Fatalf("deny 的 CONNECT：%s", status)
	}
	restricted := newSecretHarness(t, map[string]any{"environment_id": "env-py", "network": map[string]any{"mode": "restricted", "hosts": []any{
		map[string]any{"host": "api.example.com", "include_subdomains": false},
		map[string]any{"host": "example.com", "include_subdomains": true},
	}}})
	plain = restricted.client(restricted.token("egress", nil), true)
	if code, _ := restricted.get(plain, "http://api.example.com/"); code != 200 {
		t.Fatalf("名单里的主机：%d", code)
	}
	if code, _ := restricted.get(plain, "http://other.example.com/"); code != 200 {
		t.Fatalf("example.com 含子域名：%d", code)
	}
	strict := newSecretHarness(t, map[string]any{"environment_id": "env-py", "network": map[string]any{"mode": "restricted", "hosts": []any{
		map[string]any{"host": "api.example.com", "include_subdomains": false},
	}}})
	plain = strict.client(strict.token("egress", nil), true)
	if _, status := strict.tunnel(strict.token("egress", nil), "sub.api.example.com:443"); !strings.Contains(status, "403") {
		t.Fatalf("include_subdomains = false 时子域名不在名单里：%s", status)
	}
	if code, _ := strict.get(plain, "http://other.example.com/"); code != http.StatusForbidden {
		t.Fatalf("不在名单里：%d", code)
	}
	reasons := map[string]bool{}
	waitFor(t, "审计", func() bool {
		for _, a := range append(deny.audits(), strict.audits()...) {
			reasons[str(a["reason"])] = true
		}
		return reasons["environment_network_denied"] && reasons["environment_host_not_allowed"]
	})
}

func TestDeletedConfigKeepsTheNetworkSnapshotButAddsNoSecret(t *testing.T) {
	// 2026-11-03 用户定：删除配置只立即删掉机密的部分（task-service 的 DeleteEnvironmentConfig 清 environment_secrets）。
	sh := newSecretHarness(t, defaultEnvironment())
	sh.exec(`UPDATE workspaces SET environment_secrets = '', environment_deleted_at = '2026-11-03T00:00:00.000000Z' WHERE workspace_id = 'w1'`)
	sh.store.Invalidate(tenant)
	plain := sh.client(sh.token("egress", nil), true)
	// 不再终结 TLS：原样隧道到上游（沙箱按上游自己的证书校验）。
	if code, _ := sh.get(plain, "https://api.example.com/"); code != 200 {
		t.Fatalf("联网档位照快照（open）放行：%d", code)
	}
	if h, _ := sh.upstream.last(); h.Get("Authorization") != "" {
		t.Fatal("配置删除之后不再加网络密钥的头")
	}
}
