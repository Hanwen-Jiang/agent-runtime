package server_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres/pgtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1/sandboxv1connect"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/envbuild"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/server"
)

// 本包的测试用真的 sandbox-agent（本地后端每个环境起一个 agent 进程），在 TestMain 里编译一次（默认 Rust 版，D-28；
// AR_SANDBOX_AGENT_IMPL=go 时 Go 版，见 testkit.SandboxAgentBinary）。
var agentBin string

func TestMain(m *testing.M) {
	bin, err := testkit.SandboxAgentBinary()
	if err != nil {
		panic(fmt.Sprintf("编译 sandbox-agent 失败：%v", err))
	}
	agentBin = bin
	os.Exit(pgtest.Main(m))
}

const (
	workerToken = "worker-token-0123456789abcdef0123456789abcdef"
	readerToken = "task-service-token-0123456789abcdef0123456789"
)

type harness struct {
	t          *testing.T
	dir        string
	workspaces string
	state      string
	clock      *testkit.FakeClock
	store      relational.Provider
	objects    *diskobjects.Provider
	secrets    *filesecrets.Provider
	backends   string
	policies   map[string]map[string]any
	faults     map[string]*testkit.Faults
	mirror     string
	egress     string
	// wrap 按后端名包一层 Provider（测试注入后端的行为）。
	wrap map[string]func(sandbox.Provider) sandbox.Provider
	tel  telemetry.Provider
	// envs 给了时每次起服务按它建项目环境的 Manager（D-17）；env 是当前服务用的那个。
	envs func(h *harness) *envbuild.Manager
	env  *envbuild.Manager

	svc    *server.Service
	srv    *httptest.Server
	worker sandboxv1connect.SandboxServiceClient
	reader sandboxv1connect.SandboxServiceClient
}

type option func(*harness)

func withBackends(doc string) option { return func(h *harness) { h.backends = doc } }

func withPolicy(tenant string, policy map[string]any) option {
	return func(h *harness) { h.policies[tenant] = policy }
}

func withPostgres() option {
	return func(h *harness) {
		cluster := pgtest.Current(h.t)
		db := cluster.NewDatabase(h.t)
		store, err := postgres.New(context.Background(), postgres.Options{DSN: cluster.DSN(pgtest.AppRole, db), MaxConns: 16})
		if err != nil {
			h.t.Fatal(err)
		}
		h.store = store
	}
}

// withWrappedProvider 给本地后端 name 的 Provider 包一层（例如让 Exec 在取消时报沙箱没确认停下）。
func withWrappedProvider(name string, wrap func(sandbox.Provider) sandbox.Provider) option {
	return func(h *harness) {
		if h.wrap == nil {
			h.wrap = map[string]func(sandbox.Provider) sandbox.Provider{}
		}
		h.wrap[name] = wrap
	}
}

func withTelemetry(tel telemetry.Provider) option { return func(h *harness) { h.tel = tel } }

func withEnvironments(build func(h *harness) *envbuild.Manager) option {
	return func(h *harness) { h.envs = build }
}

func withGateways(mirror, egress string) option {
	return func(h *harness) { h.mirror, h.egress = mirror, egress }
}

const localMount = `{"default": "local", "backends": {"local": {"kind": "local", "isolation": "none"}}}`
const localArchive = `{"default": "local", "backends": {"local": {"kind": "local", "isolation": "none", "workspace": "archive"}}}`

func newHarness(t *testing.T, opts ...option) *harness {
	t.Helper()
	dir := t.TempDir()
	h := &harness{
		t: t, dir: dir, workspaces: filepath.Join(dir, "workspaces"), state: filepath.Join(dir, "state"), clock: testkit.NewFakeClock(),
		backends: localMount, policies: map[string]map[string]any{}, faults: map[string]*testkit.Faults{},
	}
	// 后端（本地 agent 进程）记的是真实时间：假时钟从现在开始，对账按创建时间算宽限才对得上。
	h.clock.Advance(time.Until(h.clock.Now()) * -1)
	for _, o := range opts {
		o(h)
	}
	if h.store == nil {
		store, err := sqlitestore.New(sqlitestore.Options{Dir: filepath.Join(dir, "relational")})
		if err != nil {
			t.Fatal(err)
		}
		h.store = store
	}
	objects, err := diskobjects.New(diskobjects.Options{Root: filepath.Join(dir, "objects"), SigningKey: []byte("0123456789abcdef0123456789abcdef")})
	if err != nil {
		t.Fatal(err)
	}
	h.objects = objects
	secretsFile := filepath.Join(dir, "secrets.json")
	doc := map[string]any{"version": 1, "scopes": map[string]any{
		"_platform": map[string]any{"secrets": map[string]string{
			"sandbox-service/tokens/worker":       workerToken,
			"sandbox-service/tokens/task-service": readerToken,
			"sandbox/env-credential-key":          "ZW52LWNyZWRlbnRpYWwta2V5LTAxMjM0NTY3ODlhYmNkZWY=",
		}},
		"t1": map[string]any{"secrets": map[string]string{"sandbox/test-env": `{"TEST_API_BASE": "https://api.example.test"}`}},
	}}
	raw, _ := json.Marshal(doc)
	if err := os.WriteFile(secretsFile, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	h.secrets, err = filesecrets.New(filesecrets.Options{Path: secretsFile})
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"local", "gvisor", "kata", "cloudflare", "plain", "a", "b", "firecracker", "cf"} {
		h.faults[name] = testkit.NewFaults()
	}
	h.start()
	t.Cleanup(func() {
		h.stop()
		// 本地后端的 agent 进程不随服务退出（服务重启后环境还在）：用例结束时一并收掉。
		_ = registry.KillLocalEnvironments(h.state)
		_ = h.store.Close()
	})
	return h
}

func (h *harness) start() {
	h.t.Helper()
	providers := map[string]sandbox.Provider{}
	for name, wrap := range h.wrap {
		inner, err := localsandbox.New(localsandbox.Options{Root: filepath.Join(h.state, name), Workspaces: h.workspaces, AgentBinary: agentBin, Faults: h.faults[name]})
		if err != nil {
			h.t.Fatal(err)
		}
		providers[name] = wrap(inner)
	}
	reg, err := registry.Parse([]byte(h.backends), registry.Options{
		WorkspaceRoot: h.workspaces, StateDir: h.state, AgentBinary: agentBin, Secrets: h.secrets, Faults: h.faults, Providers: providers,
	})
	if err != nil {
		h.t.Fatal(err)
	}
	h.env = nil
	if h.envs != nil {
		h.env = h.envs(h)
	}
	svc, err := server.New(context.Background(), server.Options{
		Registry: reg, Relational: h.store, Objects: h.objects, Secrets: h.secrets, Clock: h.clock, Environments: h.env,
		Policy: func(_ context.Context, tenantID string) (map[string]any, error) {
			if p, ok := h.policies[tenantID]; ok {
				return p, nil
			}
			return nil, nil
		},
		Callers:   []string{"worker", "task-service"},
		MirrorURL: h.mirror, EgressURL: h.egress, Telemetry: h.tel,
	})
	if err != nil {
		h.t.Fatal(err)
	}
	h.svc = svc
	h.srv = httptest.NewUnstartedServer(svc.Handler())
	h.srv.EnableHTTP2 = true
	h.srv.Start()
	h.worker = sandboxv1connect.NewSandboxServiceClient(h.srv.Client(), h.srv.URL, connect.WithInterceptors(caller("worker", workerToken)))
	h.reader = sandboxv1connect.NewSandboxServiceClient(h.srv.Client(), h.srv.URL, connect.WithInterceptors(caller("task-service", readerToken)))
}

func (h *harness) stop() {
	if h.srv != nil {
		h.srv.Close()
		h.srv = nil
	}
	if h.svc != nil {
		_ = h.svc.Close()
		h.svc = nil
	}
}

// restart 模拟服务被杀后重新起来：进程内的一切都丢掉，只留业务库、对象存储、工作区与后端里的环境。
func (h *harness) restart() {
	h.stop()
	h.start()
}

// caller 给每个请求（含流式）带上调用方与令牌。
func caller(name, token string) connect.Interceptor { return callerHeaders{name: name, token: token} }

type callerHeaders struct{ name, token string }

func (c callerHeaders) set(h http.Header) {
	if c.token != "" {
		h.Set("Authorization", "Bearer "+c.token)
	}
	h.Set("X-Agentruntime-Caller", c.name)
}

func (c callerHeaders) WrapUnary(next connect.UnaryFunc) connect.UnaryFunc {
	return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
		c.set(req.Header())
		return next(ctx, req)
	}
}

func (c callerHeaders) WrapStreamingClient(next connect.StreamingClientFunc) connect.StreamingClientFunc {
	return func(ctx context.Context, spec connect.Spec) connect.StreamingClientConn {
		conn := next(ctx, spec)
		c.set(conn.RequestHeader())
		return conn
	}
}

func (c callerHeaders) WrapStreamingHandler(next connect.StreamingHandlerFunc) connect.StreamingHandlerFunc {
	return next
}

func tenant(id, workspace, session string) *commonv1.TenantContext {
	return &commonv1.TenantContext{TenantId: id, UserId: "u1", WorkspaceId: workspace, SessionId: session}
}

// mkWorkspace 建挂卷的工作区目录并写入文件（相对路径 → 内容）。
func (h *harness) mkWorkspace(tenantID, workspace string, files map[string]string) string {
	h.t.Helper()
	root := filepath.Join(h.workspaces, tenantID, workspace)
	if err := os.MkdirAll(root, 0o755); err != nil {
		h.t.Fatal(err)
	}
	for rel, content := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			h.t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			h.t.Fatal(err)
		}
	}
	return root
}

type acquireOpt func(*sandboxv1.AcquireRequest)

func reuse() acquireOpt    { return func(r *sandboxv1.AcquireRequest) { r.Reuse = true } }
func readOnly() acquireOpt { return func(r *sandboxv1.AcquireRequest) { r.Writable = false } }
func busyFor(d time.Duration) acquireOpt {
	return func(r *sandboxv1.AcquireRequest) { r.BusyForMs = d.Milliseconds() }
}
func prepare() acquireOpt { return func(r *sandboxv1.AcquireRequest) { r.Prepare = true } }
func onWorkspace(ws string) acquireOpt {
	return func(r *sandboxv1.AcquireRequest) { r.Spec.WorkspaceId = ws }
}
func taskID(id string) acquireOpt { return func(r *sandboxv1.AcquireRequest) { r.TaskId = id } }

func (h *harness) acquire(tc *commonv1.TenantContext, opts ...acquireOpt) (*sandboxv1.Lease, error) {
	req := &sandboxv1.AcquireRequest{Tenant: tc, Spec: &sandboxv1.Spec{Runtime: "python"}, Writable: true}
	for _, o := range opts {
		o(req)
	}
	resp, err := h.worker.Acquire(context.Background(), connect.NewRequest(req))
	if err != nil {
		return nil, err
	}
	return resp.Msg.GetLease(), nil
}

func (h *harness) mustAcquire(tc *commonv1.TenantContext, opts ...acquireOpt) *sandboxv1.Lease {
	h.t.Helper()
	lease, err := h.acquire(tc, opts...)
	if err != nil {
		h.t.Fatalf("Acquire：%v", err)
	}
	return lease
}

func (h *harness) exec(tc *commonv1.TenantContext, lease *sandboxv1.Lease, script string) (*sandboxv1.ExecResponse, error) {
	resp, err := h.worker.Exec(context.Background(), connect.NewRequest(&sandboxv1.ExecRequest{
		Tenant: tc, LeaseId: lease.GetLeaseId(), Argv: []string{"sh", "-c", script}, TimeoutMs: 30_000,
	}))
	if err != nil {
		return nil, err
	}
	return resp.Msg, nil
}

func (h *harness) mustExec(tc *commonv1.TenantContext, lease *sandboxv1.Lease, script string) string {
	h.t.Helper()
	out, err := h.exec(tc, lease, script)
	if err != nil {
		h.t.Fatalf("Exec %q：%v", script, err)
	}
	if out.GetExitCode() != 0 || out.GetTimedOut() {
		h.t.Fatalf("Exec %q：退出码 %d，stderr %s", script, out.GetExitCode(), out.GetStderr())
	}
	return string(out.GetStdout())
}

func (h *harness) release(tc *commonv1.TenantContext, lease *sandboxv1.Lease, keep bool) (*sandboxv1.Lease, error) {
	resp, err := h.worker.Release(context.Background(), connect.NewRequest(&sandboxv1.ReleaseRequest{Tenant: tc, LeaseId: lease.GetLeaseId(), Keep: keep}))
	if err != nil {
		return nil, err
	}
	return resp.Msg.GetLease(), nil
}

// rows 在这个作用域的业务库里查（只给测试核对环境表）。
func (h *harness) rows(owner, sql string, args ...any) []relational.Row {
	h.t.Helper()
	var store relational.Store
	var err error
	if owner == "_platform" {
		store, err = h.store.BindPlatform("sandbox")
	} else {
		store, err = h.store.Bind(&commonv1.TenantContext{TenantId: owner})
	}
	if err != nil {
		h.t.Fatal(err)
	}
	var out []relational.Row
	err = store.Transaction(context.Background(), relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		out, err = tx.Query(ctx, sql, args...)
		return err
	})
	if err != nil {
		h.t.Fatalf("%s：%v", sql, err)
	}
	return out
}

func (h *harness) execSQL(owner, sql string, args ...any) {
	h.t.Helper()
	store, err := h.store.Bind(&commonv1.TenantContext{TenantId: owner})
	if err != nil {
		h.t.Fatal(err)
	}
	err = store.Transaction(context.Background(), relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, sql, args...)
		return err
	})
	if err != nil {
		h.t.Fatalf("%s：%v", sql, err)
	}
}

func (h *harness) envCount(owner string) int {
	return len(h.rows(owner, "SELECT env_ref FROM sandbox_envs"))
}

func (h *harness) envState(owner, ref string) string {
	rows := h.rows(owner, "SELECT state FROM sandbox_envs WHERE env_ref = $1", ref)
	if len(rows) != 1 {
		h.t.Fatalf("环境表里没有 %s", ref)
	}
	return fmt.Sprint(rows[0]["state"])
}

// code 取出统一错误模型的类别与错误码。
func code(t *testing.T, err error) (providererr.Category, string) {
	t.Helper()
	if err == nil {
		t.Fatal("要报错，没有报错")
	}
	pe := connecterr.Decode(err)
	if pe == nil {
		t.Fatalf("不是统一错误模型的错误：%v", err)
	}
	return pe.Category, pe.Code
}

func connectCode(err error) connect.Code {
	var ce *connect.Error
	if errors.As(err, &ce) {
		return ce.Code()
	}
	return connect.CodeUnknown
}

func contains(haystack, needle string) bool { return strings.Contains(haystack, needle) }

// box 是直接绑定到某个后端的沙箱（绕过服务，造孤儿、模拟后端里的环境没了）。
func (h *harness) box(backend, tenantID string) sandbox.Sandbox {
	h.t.Helper()
	provider, err := h.svc.Registry().Provider(context.Background(), backend)
	if err != nil {
		h.t.Fatal(err)
	}
	box, err := provider.Bind(&commonv1.TenantContext{TenantId: tenantID})
	if err != nil {
		h.t.Fatal(err)
	}
	return box
}

func gitCmd(dir string, args ...string) *exec.Cmd {
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_CONFIG_NOSYSTEM=1", "HOME="+dir)
	return cmd
}
