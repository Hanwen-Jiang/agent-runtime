package proxy

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"

	"github.com/agentruntime/agent-runtime/services/go-egress-gateway/internal/grants"
)

// workerTables 与 Worker 的迁移（agentruntime_worker.sandbox.ledger 的 201～203）里网关碰到的表一致。
var workerTables = []relational.Migration{
	{Version: 201, Name: "sandbox_envs", Statements: []string{
		`CREATE TABLE sandbox_envs (
			env_ref TEXT PRIMARY KEY, backend TEXT NOT NULL, kind TEXT NOT NULL, isolation TEXT NOT NULL,
			backend_env_id TEXT NOT NULL DEFAULT '', workspace_id TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
			task_id TEXT NOT NULL DEFAULT '', writable BIGINT NOT NULL DEFAULT 0, runtime TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '',
			state TEXT NOT NULL, created_at TEXT NOT NULL, ready_at TEXT NOT NULL DEFAULT '', last_used_at TEXT NOT NULL DEFAULT '',
			expires_at TEXT NOT NULL DEFAULT '', max_until TEXT NOT NULL DEFAULT '', destroyed_at TEXT NOT NULL DEFAULT '',
			idle_seconds BIGINT NOT NULL DEFAULT 0, busy_until TEXT NOT NULL DEFAULT '', cpu_ms BIGINT NOT NULL DEFAULT 0,
			alive_ms BIGINT NOT NULL DEFAULT 0, cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0, fallback_from TEXT NOT NULL DEFAULT '',
			reason TEXT NOT NULL DEFAULT '')`,
		`CREATE TABLE sandbox_usage (day TEXT PRIMARY KEY, sandbox_seconds BIGINT NOT NULL DEFAULT 0, egress_bytes BIGINT NOT NULL DEFAULT 0,
			mirror_miss_bytes BIGINT NOT NULL DEFAULT 0, cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0)`,
	}},
	{Version: 202, Name: "sandbox_grants", Statements: []string{
		`CREATE TABLE sandbox_grants (
			grant_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workspace_id TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
			hosts TEXT NOT NULL, ports TEXT NOT NULL, expires_at TEXT NOT NULL, max_up_bytes BIGINT NOT NULL DEFAULT 0,
			max_down_bytes BIGINT NOT NULL DEFAULT 0, max_conns BIGINT NOT NULL DEFAULT 0, max_rps BIGINT NOT NULL DEFAULT 0,
			up_bytes BIGINT NOT NULL DEFAULT 0, down_bytes BIGINT NOT NULL DEFAULT 0, preview_port BIGINT NOT NULL DEFAULT 0,
			state TEXT NOT NULL, approved_by TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
			revoked_at TEXT NOT NULL DEFAULT '')`,
	}},
	{Version: 203, Name: "sandbox_egress", Statements: []string{
		`ALTER TABLE sandbox_grants ADD COLUMN preview_env TEXT NOT NULL DEFAULT ''`,
		`ALTER TABLE sandbox_grants ADD COLUMN preview_target TEXT NOT NULL DEFAULT ''`,
		`ALTER TABLE sandbox_envs ADD COLUMN allow_hosts TEXT NOT NULL DEFAULT ''`,
		`CREATE TABLE sandbox_egress_audit (
			audit_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '', env_ref TEXT NOT NULL,
			direction TEXT NOT NULL, host TEXT NOT NULL, port BIGINT NOT NULL DEFAULT 0, address TEXT NOT NULL DEFAULT '',
			started_at TEXT NOT NULL, duration_ms BIGINT NOT NULL DEFAULT 0, up_bytes BIGINT NOT NULL DEFAULT 0,
			down_bytes BIGINT NOT NULL DEFAULT 0, result TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '')`,
	}},
}

const (
	tenant  = "t-net"
	envRef  = "sbe-00112233445566aa"
	taskID  = "tsk_01HZY2J5K3M4N5P6Q7R8S9T0VW"
	adminTk = "admin-token-for-tests"
)

var (
	publicAPI   = netip.MustParseAddr("93.184.216.34")
	publicOther = netip.MustParseAddr("93.184.216.35")
)

type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

type fakeResolver struct {
	mu      sync.Mutex
	answers map[string][]netip.Addr
	asked   []string
}

func (r *fakeResolver) LookupNetIP(_ context.Context, _, host string) ([]netip.Addr, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.asked = append(r.asked, host)
	if a, ok := r.answers[host]; ok {
		return a, nil
	}
	return nil, &net.DNSError{Err: "no such host", Name: host, IsNotFound: true}
}

// fakeDialer 把“公网”地址映射到本机的假服务；记下拨过的地址。
type fakeDialer struct {
	mu     sync.Mutex
	routes map[string]string
	dialed []string
}

func (d *fakeDialer) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	d.mu.Lock()
	d.dialed = append(d.dialed, address)
	target, ok := d.routes[address]
	d.mu.Unlock()
	if !ok {
		return nil, errors.New("测试里没有这个地址：" + address)
	}
	var dialer net.Dialer
	return dialer.DialContext(ctx, network, target)
}

func (d *fakeDialer) dialedTo(prefix string) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	for _, a := range d.dialed {
		if strings.HasPrefix(a, prefix) {
			return true
		}
	}
	return false
}

type harness struct {
	t        *testing.T
	key      []byte
	db       relational.Provider
	store    *grants.Store
	gw       *Gateway
	clock    *fakeClock
	resolver *fakeResolver
	dialer   *fakeDialer
	proxy    *httptest.Server
	admin    *httptest.Server
	preview  *httptest.Server
	echo     net.Listener
}

func upstreamHandler(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/redirect":
		http.Redirect(w, r, "http://169.254.169.254/latest/meta-data/", http.StatusFound)
	case "/upload":
		n, _ := io.Copy(io.Discard, r.Body)
		fmt.Fprintf(w, "got %d", n)
	case "/big":
		_, _ = w.Write([]byte(strings.Repeat("x", 64*1024)))
	default:
		fmt.Fprintf(w, "hello from %s", r.Host)
	}
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	db, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := db.Migrate(context.Background(), workerTables); err != nil {
		t.Fatal(err)
	}
	h := &harness{t: t, key: []byte(strings.Repeat("k", 32)), db: db, clock: &fakeClock{t: time.Now().UTC()}}
	httpSrv := httptest.NewServer(http.HandlerFunc(upstreamHandler))
	tlsSrv := httptest.NewTLSServer(http.HandlerFunc(upstreamHandler))
	t.Cleanup(httpSrv.Close)
	t.Cleanup(tlsSrv.Close)
	h.echo, err = net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = h.echo.Close() })
	go func() {
		for {
			c, err := h.echo.Accept()
			if err != nil {
				return
			}
			go func() { _, _ = io.Copy(c, c); _ = c.Close() }()
		}
	}()
	h.resolver = &fakeResolver{answers: map[string][]netip.Addr{
		"api.example.com":    {publicAPI},
		"other.example.com":  {publicOther},
		"rebind.example.com": {publicAPI, netip.MustParseAddr("10.0.0.7")},
		"meta.example.com":   {netip.MustParseAddr("169.254.169.254")},
		"nat.example.com":    {netip.MustParseAddr("::ffff:10.1.2.3")},
		"v6.example.com":     {netip.MustParseAddr("fd00::1")},
	}}
	h.dialer = &fakeDialer{routes: map[string]string{
		"93.184.216.34:80":   httpSrv.Listener.Addr().String(),
		"93.184.216.34:443":  tlsSrv.Listener.Addr().String(),
		"93.184.216.34:7000": h.echo.Addr().String(),
		"93.184.216.35:80":   httpSrv.Listener.Addr().String(),
	}}
	h.store = grants.New(db, 200*time.Millisecond, h.clock.Now)
	h.gw = New(Config{
		Key: h.key, Store: h.store, Resolver: h.resolver, Dialer: h.dialer, Now: h.clock.Now, CheckInterval: 100 * time.Millisecond,
		PreviewDomain: "preview.example.test", PreviewRPS: 8,
	})
	h.proxy = httptest.NewServer(h.gw)
	h.admin = httptest.NewServer(h.gw.Admin(adminTk))
	h.preview = httptest.NewServer(h.gw.Preview())
	t.Cleanup(h.proxy.Close)
	t.Cleanup(h.admin.Close)
	t.Cleanup(h.preview.Close)
	h.env(envRef, taskID, "ready")
	return h
}

func (h *harness) exec(sql string, args ...any) {
	h.t.Helper()
	db, err := h.db.Bind(&commonv1.TenantContext{TenantId: tenant})
	if err != nil {
		h.t.Fatal(err)
	}
	if err := db.Transaction(context.Background(), relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, sql, args...)
		return err
	}); err != nil {
		h.t.Fatal(err)
	}
}

func (h *harness) query(sql string, args ...any) []relational.Row {
	h.t.Helper()
	db, _ := h.db.Bind(&commonv1.TenantContext{TenantId: tenant})
	var rows []relational.Row
	if err := db.Transaction(context.Background(), relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var err error
		rows, err = tx.Query(ctx, sql, args...)
		return err
	}); err != nil {
		h.t.Fatal(err)
	}
	return rows
}

func (h *harness) env(ref, task, state string) {
	h.exec(`INSERT INTO sandbox_envs (env_ref, backend, kind, isolation, task_id, state, created_at) VALUES ($1, 'gvisor', 'k8s', 'container', $2, $3, $4)
		ON CONFLICT (env_ref) DO UPDATE SET task_id = excluded.task_id, state = excluded.state`, ref, task, state, grants.ISO(h.clock.Now()))
}

type grantSpec struct {
	id, task             string
	hosts                []string
	ports                []int
	minutes              int
	maxUp, maxConns, rps int64
	previewEnv           string
	previewPort          int
	previewTarget        string
}

func (h *harness) grant(g grantSpec) {
	if g.task == "" {
		g.task = taskID
	}
	if g.minutes == 0 {
		g.minutes = 15
	}
	if g.ports == nil {
		g.ports = []int{80, 443}
	}
	hosts, _ := json.Marshal(g.hosts)
	ports, _ := json.Marshal(g.ports)
	h.exec(`INSERT INTO sandbox_grants (grant_id, task_id, hosts, ports, expires_at, max_up_bytes, max_conns, max_rps, state, approved_by, created_at,
		preview_env, preview_port, preview_target) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'active', 'u-approver', $9, $10, $11, $12)`,
		g.id, g.task, string(hosts), string(ports), grants.ISO(h.clock.Now().Add(time.Duration(g.minutes)*time.Minute)), g.maxUp, g.maxConns, g.rps,
		grants.ISO(h.clock.Now()), g.previewEnv, int64(g.previewPort), g.previewTarget)
}

func (h *harness) token(purpose string, limits map[string]int64) string {
	h.t.Helper()
	tok, err := envcred.Issue(h.key, envcred.Claims{EnvRef: envRef, TenantID: tenant, ExpiresAt: h.clock.Now().Add(6 * time.Hour).Unix(), Purpose: purpose, Limits: limits})
	if err != nil {
		h.t.Fatal(err)
	}
	return tok
}

// client 是沙箱里的程序：经代理、带环境凭据；不跟随重定向时 noRedirect = true。
func (h *harness) client(token string, noRedirect bool) *http.Client {
	u, _ := url.Parse(h.proxy.URL)
	if token != "" {
		u.User = url.UserPassword("ar", token)
	}
	c := &http.Client{
		Timeout:   10 * time.Second,
		Transport: &http.Transport{Proxy: http.ProxyURL(u), TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, DisableKeepAlives: true},
	}
	if noRedirect {
		c.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	}
	return c
}

func (h *harness) get(c *http.Client, rawURL string) (int, string) {
	h.t.Helper()
	resp, err := c.Get(rawURL)
	if err != nil {
		return 0, err.Error()
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}

// tunnel 开一条 CONNECT 隧道（原始 TCP），返回连接与代理的应答行。
func (h *harness) tunnel(token, target string) (net.Conn, string) {
	h.t.Helper()
	conn, err := net.Dial("tcp", h.proxy.Listener.Addr().String())
	if err != nil {
		h.t.Fatal(err)
	}
	auth := "ar:" + token
	fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: Basic %s\r\n\r\n", target, target, b64(auth))
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	buf := make([]byte, 0, 512)
	one := make([]byte, 1)
	for !strings.HasSuffix(string(buf), "\r\n\r\n") {
		if _, err := conn.Read(one); err != nil {
			break
		}
		buf = append(buf, one[0])
	}
	_ = conn.SetReadDeadline(time.Time{})
	return conn, strings.SplitN(string(buf), "\r\n", 2)[0]
}

func (h *harness) audits() []relational.Row {
	return h.query("SELECT host, port, address, result, reason, up_bytes, down_bytes, duration_ms, task_id, grant_id, direction FROM sandbox_egress_audit ORDER BY started_at, audit_id")
}

// waitFor 等条件成立（审计是在连接结束时写的）。
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("等不到：%s", what)
}
