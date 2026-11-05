// Package server 是 Sandbox Service 的 gRPC 服务（connect，h2c；SBX-01）。契约见 proto/agentruntime/sandbox/v1/sandbox.proto。
//
//   - 认证：每个调用方一个服务令牌（SecretStore 平台作用域的 sandbox-service/tokens/<调用方>），请求头 authorization: Bearer <令牌>
//     与 x-agentruntime-caller: <调用方>；比对用常量时间。task-service 只能调只读的工作区方法。
//   - 租户：每个请求的字段 1 是 TenantContext，缺了 denied（missing_tenant），格式不对 invalid；服务按它绑定所有 Provider。
//   - 错误：统一错误模型（connecterr），错误码原样保留。
//   - 平台注入的环境变量只在服务里：Lease 只给测试用密钥的名字。
package server

import (
	"context"
	"crypto/subtle"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	environmentv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/environment/v1"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1/sandboxv1connect"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/archive"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/broker"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/envbuild"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
)

// TokenSecretPrefix 是调用方令牌在 SecretStore 平台作用域里的名字前缀。
const TokenSecretPrefix = "sandbox-service/tokens/"

// CallerHeader 是调用方的名字。
const CallerHeader = "X-Agentruntime-Caller"

// ReadOnlyCaller 是只能调只读方法的调用方。
const ReadOnlyCaller = "task-service"

var readOnly = map[string]bool{
	sandboxv1connect.SandboxServiceListFilesProcedure: true, sandboxv1connect.SandboxServiceReadFileProcedure: true,
	sandboxv1connect.SandboxServiceGrepProcedure: true, sandboxv1connect.SandboxServiceGetDiffProcedure: true,
	sandboxv1connect.SandboxServiceGetLogProcedure: true, sandboxv1connect.SandboxServiceDescribeTenantProcedure: true,
}

// Options 是构造参数。
type Options struct {
	Registry   *registry.Registry
	Relational relational.Provider
	Objects    objectstore.Provider
	Secrets    secretstore.Provider
	Policy     registry.Lookup
	Clock      clock.Clock
	// Callers 是登记的调用方（worker、task-service）。
	Callers   []string
	MirrorURL string
	EgressURL string
	// Runtime 是服务自己借环境（git、转冷）时用的运行环境；空时 python。
	Runtime    string
	HoldMax    time.Duration
	Grace      time.Duration
	ColdDays   int
	PolicyTTL  time.Duration
	TmpDir     string
	SetupCache broker.SetupCache
	Projects   broker.ProjectEnvironments
	// Environments 是按项目定制的运行环境（D-17，后端登记的 environments 一节）；给了时它同时是 SetupCache 与 Projects，
	// 构建在服务进程里跑。
	Environments *envbuild.Manager
	Logger       *slog.Logger
	MaxMsgBytes  int
	// Telemetry 给了时每轮对账报一次卷用量（runtime.workspace.volume_used_ratio，DISK-A）；没给时只在报告里。
	Telemetry telemetry.Provider
}

// Service 是一个服务实例。
type Service struct {
	opts    Options
	broker  *broker.Broker
	handler http.Handler
	log     *slog.Logger

	mu     sync.Mutex
	tokens map[string][]byte
	loaded time.Time
}

// New 构造服务：建表（迁移归服务），读调用方的令牌。
func New(ctx context.Context, o Options) (*Service, error) {
	if o.Registry == nil || o.Relational == nil {
		return nil, errors.New("sandbox service: Registry 与 Relational 必填")
	}
	if o.Clock == nil {
		o.Clock = clock.System{}
	}
	if o.Runtime == "" {
		o.Runtime = "python"
	}
	if o.HoldMax == 0 {
		o.HoldMax = broker.DefaultHoldMax
	}
	if o.Grace == 0 {
		o.Grace = 10 * time.Minute
	}
	if o.PolicyTTL == 0 {
		o.PolicyTTL = time.Minute
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	if o.MaxMsgBytes == 0 {
		o.MaxMsgBytes = 64 << 20
	}
	if o.Clock != (clock.System{}) {
		// 测试用的时钟：策略不缓存，免得时钟前进后还用旧的。
		o.PolicyTTL = -1
	}
	led := ledger.New(o.Relational, o.Clock)
	arch := archive.New(o.Relational, o.Objects, o.Clock)
	arch.TmpDir = o.TmpDir
	if err := led.Migrate(ctx); err != nil {
		return nil, err
	}
	if err := arch.Migrate(ctx); err != nil {
		return nil, err
	}
	reg := o.Registry
	if o.Environments != nil {
		o.SetupCache, o.Projects = o.Environments, o.Environments
	}
	b := broker.New(broker.Options{
		Registry: reg, Policies: registry.NewPolicies(reg, o.Policy, o.PolicyTTL), Ledger: led, Archiver: arch, Store: o.Relational,
		Credentials: broker.NewCredentials(reg, o.MirrorURL, o.EgressURL), SetupCache: o.SetupCache, Projects: o.Projects, HoldMax: o.HoldMax,
		EgressCA: func(ctx context.Context) []byte { v, _ := reg.ReadSecret(ctx, "sandbox/egress-ca-cert"); return v }, Logger: o.Logger,
	})
	b.SetCold(broker.ColdOptions{DefaultDays: o.ColdDays, Runtime: o.Runtime})
	if o.Environments != nil {
		o.Environments.Attach(b)
	}
	s := &Service{opts: o, broker: b, log: o.Logger, tokens: map[string][]byte{}}
	if err := s.loadTokens(ctx); err != nil {
		return nil, err
	}
	path, h := sandboxv1connect.NewSandboxServiceHandler(&handler{s: s}, connect.WithInterceptors(&auth{s: s}),
		connect.WithReadMaxBytes(o.MaxMsgBytes), connect.WithSendMaxBytes(o.MaxMsgBytes))
	mux := http.NewServeMux()
	mux.Handle(path, h)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") })
	s.handler = mux
	return s, nil
}

// Handler 是服务的 HTTP 处理器（gRPC 与 /healthz）。
func (s *Service) Handler() http.Handler { return s.handler }

// Registry 是后端登记。
func (s *Service) Registry() *registry.Registry { return s.opts.Registry }

// Broker 是借还。
func (s *Service) Broker() *broker.Broker { return s.broker }

// Reconcile 跑一轮对账。
func (s *Service) Reconcile(ctx context.Context) (broker.Report, error) {
	report, err := s.broker.Reconcile(ctx, s.opts.Grace, s.opts.HoldMax)
	if err == nil && report.VolumeUsedRatio >= 0 && s.opts.Telemetry != nil {
		if tel, berr := s.opts.Telemetry.BindPlatform("sandbox-reconcile"); berr == nil {
			_ = tel.Observe(ctx, broker.VolumeMetric, report.VolumeUsedRatio, telemetry.Attribute{Key: "volume", Value: "workspaces"})
			_ = tel.Flush(ctx)
		}
	}
	return report, err
}

// Close 收尾（本地后端的环境不随服务退出；进程里没跑完的构建留着 building，重启后由用到它的 Acquire 接手）。
func (s *Service) Close() error {
	if s.opts.Environments != nil {
		s.opts.Environments.Close()
	}
	return s.opts.Registry.Close()
}

func (s *Service) loadTokens(ctx context.Context) error {
	tokens := map[string][]byte{}
	for _, c := range s.opts.Callers {
		v, err := s.opts.Registry.ReadSecret(ctx, TokenSecretPrefix+c)
		if err != nil {
			return err
		}
		if len(v) >= 32 {
			tokens[c] = v
		} else if len(v) > 0 {
			s.log.Warn("调用方的令牌太短（至少 32 个字符），不接受它", "caller", c)
		}
	}
	s.mu.Lock()
	s.tokens, s.loaded = tokens, time.Now()
	s.mu.Unlock()
	return nil
}

func (s *Service) check(ctx context.Context, caller, authz, procedure string) error {
	token := strings.TrimPrefix(authz, "Bearer ")
	if caller == "" || token == "" || token == authz {
		return connect.NewError(connect.CodeUnauthenticated, errors.New("缺少调用方或令牌"))
	}
	s.mu.Lock()
	want, ok := s.tokens[caller]
	stale := time.Since(s.loaded) > 30*time.Second
	s.mu.Unlock()
	if (!ok || subtle.ConstantTimeCompare(want, []byte(token)) != 1) && stale {
		// 令牌轮换：最多每 30 秒重新读一次。
		if err := s.loadTokens(ctx); err == nil {
			s.mu.Lock()
			want, ok = s.tokens[caller]
			s.mu.Unlock()
		}
	}
	if !ok || subtle.ConstantTimeCompare(want, []byte(token)) != 1 {
		return connect.NewError(connect.CodeUnauthenticated, errors.New("调用方的令牌不对"))
	}
	if caller == ReadOnlyCaller && !readOnly[procedure] {
		return connect.NewError(connect.CodePermissionDenied, errors.New("task-service 只能调只读的工作区方法"))
	}
	return nil
}

type auth struct{ s *Service }

func (a *auth) WrapUnary(next connect.UnaryFunc) connect.UnaryFunc {
	return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
		if err := a.s.check(ctx, req.Header().Get(CallerHeader), req.Header().Get("Authorization"), req.Spec().Procedure); err != nil {
			return nil, err
		}
		return next(ctx, req)
	}
}

func (a *auth) WrapStreamingClient(next connect.StreamingClientFunc) connect.StreamingClientFunc {
	return next
}

func (a *auth) WrapStreamingHandler(next connect.StreamingHandlerFunc) connect.StreamingHandlerFunc {
	return func(ctx context.Context, conn connect.StreamingHandlerConn) error {
		h := conn.RequestHeader()
		if err := a.s.check(ctx, h.Get(CallerHeader), h.Get("Authorization"), conn.Spec().Procedure); err != nil {
			return err
		}
		return next(ctx, conn)
	}
}

type handler struct{ s *Service }

func fail(err error) error {
	var ce *connect.Error
	if errors.As(err, &ce) {
		return ce
	}
	return connecterr.Error(err)
}

// tenant 核对租户上下文（服务端构造的，调用方按认证身份填写）。
func tenant(t *commonv1.TenantContext) (*commonv1.TenantContext, error) {
	if _, err := tenancy.TenantScope(t); err != nil {
		return nil, err
	}
	return t, nil
}

func (h *handler) b() *broker.Broker { return h.s.broker }

// --- 只读 ---

func (h *handler) ListFiles(ctx context.Context, req *connect.Request[sandboxv1.ListFilesRequest]) (*connect.Response[sandboxv1.ListFilesResponse], error) {
	t, err := tenant(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	items, truncated, err := h.b().ListWorkspace(ctx, t, req.Msg.GetWorkspaceId(), req.Msg.GetPath())
	if err != nil {
		return nil, fail(err)
	}
	out := &workspacev1.ListFilesResponse{Truncated: truncated}
	for _, it := range items {
		e := &workspacev1.FileEntry{Name: it.Name, Type: fileType(it.Type)}
		if it.Type == "file" {
			e.Size = it.Size
		}
		out.Entries = append(out.Entries, e)
	}
	return connect.NewResponse(&sandboxv1.ListFilesResponse{Response: out}), nil
}

func fileType(t string) workspacev1.FileType {
	switch t {
	case "file":
		return workspacev1.FileType_FILE_TYPE_FILE
	case "dir":
		return workspacev1.FileType_FILE_TYPE_DIRECTORY
	case "symlink":
		return workspacev1.FileType_FILE_TYPE_SYMLINK
	}
	return workspacev1.FileType_FILE_TYPE_UNSPECIFIED
}

func (h *handler) ReadFile(ctx context.Context, req *connect.Request[sandboxv1.ReadFileRequest]) (*connect.Response[sandboxv1.ReadFileResponse], error) {
	t, err := tenant(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	r, err := h.b().ReadFile(ctx, t, req.Msg.GetWorkspaceId(), req.Msg.GetPath())
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.ReadFileResponse{Response: &workspacev1.ReadFileResponse{
		Size: r.Size, TooLarge: r.TooLarge, Binary: r.Binary, Content: r.Content}}), nil
}

func grepMatches(ms []sandbox.GrepMatch) []*sandboxv1.GrepMatch {
	out := make([]*sandboxv1.GrepMatch, 0, len(ms))
	for _, m := range ms {
		out = append(out, &sandboxv1.GrepMatch{Path: m.Path, Line: int32(m.Line), Text: m.Text})
	}
	return out
}

func (h *handler) Grep(ctx context.Context, req *connect.Request[sandboxv1.GrepRequest]) (*connect.Response[sandboxv1.GrepResponse], error) {
	t, err := tenant(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	ms, truncated, err := h.b().GrepWorkspace(ctx, t, req.Msg.GetWorkspaceId(), req.Msg.GetPattern(), req.Msg.GetPath(), int(req.Msg.GetMaxMatches()))
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.GrepResponse{Matches: grepMatches(ms), Truncated: truncated}), nil
}

func (h *handler) GetDiff(ctx context.Context, req *connect.Request[sandboxv1.GetDiffRequest]) (*connect.Response[sandboxv1.GetDiffResponse], error) {
	t, err := tenant(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	d, err := h.b().Diff(ctx, t, req.Msg.GetWorkspaceId(), req.Msg.GetBase(), req.Msg.GetFindRenames(), h.s.opts.Runtime)
	if err != nil {
		return nil, fail(err)
	}
	out := &workspacev1.GetDiffResponse{Diff: d.Diff, Files: d.Files, Truncated: d.Truncated, Head: d.Head, Branch: d.Branch}
	for _, st := range d.Stats {
		out.Stats = append(out.Stats, &workspacev1.DiffStat{Path: st.Path, Added: st.Added, Deleted: st.Deleted, Binary: st.Binary})
	}
	return connect.NewResponse(&sandboxv1.GetDiffResponse{Response: out}), nil
}

func (h *handler) GetLog(ctx context.Context, req *connect.Request[sandboxv1.GetLogRequest]) (*connect.Response[sandboxv1.GetLogResponse], error) {
	t, err := tenant(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	commits, err := h.b().Log(ctx, t, req.Msg.GetWorkspaceId(), int(req.Msg.GetLimit()), req.Msg.GetBase(), h.s.opts.Runtime)
	if err != nil {
		return nil, fail(err)
	}
	out := &workspacev1.GetLogResponse{}
	for _, c := range commits {
		pc := &workspacev1.Commit{Sha: c.SHA, AuthorName: c.AuthorName, AuthorEmail: c.AuthorEmail, Subject: c.Subject}
		if at, err := time.Parse(time.RFC3339, c.AuthoredAt); err == nil {
			pc.AuthoredAt = timestamppb.New(at)
		}
		out.Commits = append(out.Commits, pc)
	}
	return connect.NewResponse(&sandboxv1.GetLogResponse{Response: out}), nil
}

func (h *handler) DescribeTenant(ctx context.Context, req *connect.Request[sandboxv1.DescribeTenantRequest]) (*connect.Response[sandboxv1.DescribeTenantResponse], error) {
	t, err := tenant(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	p, err := h.b().Policies().For(ctx, t.GetTenantId())
	if err != nil {
		return nil, fail(err)
	}
	// Worker 先问这个，再往同一个业务库写它自己的记录（联网授权、工作区）：开发时每个租户一个 SQLite 文件，读一次就把这个
	// 租户的表建好（结构变更归服务）；Postgres 上只是一次小查询。
	if _, err := h.b().Ledger().Active(ctx, t.GetTenantId()); err != nil {
		return nil, fail(err)
	}
	reg := h.s.opts.Registry
	return connect.NewResponse(&sandboxv1.DescribeTenantResponse{
		Backends: p.Names(), ArchiveMode: reg.Archive(p.Backend), WorkspaceMaxBytes: p.Workspace.MaxBytes, MaxWorkspaces: int32(p.Workspace.MaxWorkspaces),
		Gateway: h.b().HasGateway(p.Backend), Kind: reg.Kind(p.Backend), Isolation: reg.Isolation(p.Backend),
	}), nil
}

// --- 借还 ---

func (h *handler) Acquire(ctx context.Context, req *connect.Request[sandboxv1.AcquireRequest]) (*connect.Response[sandboxv1.AcquireResponse], error) {
	t, err := tenant(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	m, spec := req.Msg, req.Msg.GetSpec()
	if spec.GetWorkspaceId() != "" {
		if err := sandbox.CheckWorkspaceID(spec.GetWorkspaceId()); err != nil {
			return nil, fail(err)
		}
	}
	a := broker.AcquireArgs{
		Spec: sandbox.Spec{Runtime: spec.GetRuntime(), CPUMillis: int(spec.GetCpuMillis()), MemoryMB: int(spec.GetMemoryMb()),
			IdleTimeout: time.Duration(spec.GetIdleTimeoutS()) * time.Second, WorkspaceID: spec.GetWorkspaceId(), Image: spec.GetImage()},
		TaskID: m.GetTaskId(), Writable: m.GetWritable(), Reuse: m.GetReuse(), Prepare: m.GetPrepare(), Cooling: m.GetCooling(), Project: m.GetProjectEnvironment(),
		Flush: m.GetFlushWritable(),
	}
	if m.GetBusyForMs() > 0 {
		d := time.Duration(m.GetBusyForMs()) * time.Millisecond
		a.BusyFor = &d
	}
	lease, err := h.b().Acquire(ctx, t, a)
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.AcquireResponse{Lease: h.s.leaseProto(lease)}), nil
}

func (h *handler) lease(ctx context.Context, t *commonv1.TenantContext, id string) (*broker.Lease, error) {
	t, err := tenant(t)
	if err != nil {
		return nil, err
	}
	return h.b().Get(ctx, t, id)
}

func (h *handler) Exec(ctx context.Context, req *connect.Request[sandboxv1.ExecRequest]) (*connect.Response[sandboxv1.ExecResponse], error) {
	m := req.Msg
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	timeout := time.Duration(m.GetTimeoutMs()) * time.Millisecond
	if timeout <= 0 {
		timeout = time.Minute
	}
	res, err := h.b().Exec(ctx, lease, m.GetArgv(), timeout, m.GetStdin(), m.GetEnv())
	if err != nil {
		return nil, fail(err)
	}
	limit := m.GetMaxOutputBytes()
	if limit <= 0 {
		limit = 4 << 20
	}
	out := &sandboxv1.ExecResponse{TimedOut: res.TimedOut, CpuTimeMs: -1}
	out.Stdout, out.StdoutTruncated = tail(res.Stdout, limit)
	out.Stderr, out.StderrTruncated = tail(res.Stderr, limit)
	if res.Truncated {
		out.StdoutTruncated, out.StderrTruncated = true, true
	}
	if res.ExitCode != nil {
		out.HasExitCode, out.ExitCode = true, int32(*res.ExitCode)
	}
	if res.CPUTime >= 0 {
		out.CpuTimeMs = res.CPUTime.Milliseconds()
	}
	return connect.NewResponse(out), nil
}

func tail(b []byte, limit int64) ([]byte, bool) {
	if int64(len(b)) <= limit {
		return b, false
	}
	return b[int64(len(b))-limit:], true
}

func (h *handler) WriteFile(ctx context.Context, req *connect.Request[sandboxv1.WriteFileRequest]) (*connect.Response[sandboxv1.WriteFileResponse], error) {
	m := req.Msg
	if err := sandbox.CheckPath(m.GetPath()); err != nil {
		return nil, fail(err)
	}
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	if err := lease.Box.WriteFile(ctx, lease.EnvID, m.GetPath(), m.GetData()); err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.WriteFileResponse{}), nil
}

func (h *handler) ReadLeaseFile(ctx context.Context, req *connect.Request[sandboxv1.ReadLeaseFileRequest]) (*connect.Response[sandboxv1.ReadLeaseFileResponse], error) {
	m := req.Msg
	if err := sandbox.CheckPath(m.GetPath()); err != nil {
		return nil, fail(err)
	}
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	data, err := lease.Box.ReadFile(ctx, lease.EnvID, m.GetPath())
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.ReadLeaseFileResponse{Data: data}), nil
}

func browse(lease *broker.Lease) (sandbox.Browse, error) {
	b, ok := lease.Box.(sandbox.Browse)
	if !ok {
		return nil, providererr.New(providererr.Invalid, "unsupported_browse", "这个沙箱后端不能在环境里列目录、搜索")
	}
	return b, nil
}

func (h *handler) ListLeaseDir(ctx context.Context, req *connect.Request[sandboxv1.ListLeaseDirRequest]) (*connect.Response[sandboxv1.ListLeaseDirResponse], error) {
	m := req.Msg
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	br, err := browse(lease)
	if err != nil {
		return nil, fail(err)
	}
	items, err := br.ListDir(ctx, lease.EnvID, m.GetPath())
	if err != nil {
		return nil, fail(err)
	}
	out := &sandboxv1.ListLeaseDirResponse{}
	for _, it := range items {
		out.Entries = append(out.Entries, &sandboxv1.DirEntry{Name: it.Name, Type: it.Type, Size: it.Size})
	}
	return connect.NewResponse(out), nil
}

func (h *handler) GrepLease(ctx context.Context, req *connect.Request[sandboxv1.GrepLeaseRequest]) (*connect.Response[sandboxv1.GrepLeaseResponse], error) {
	m := req.Msg
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	br, err := browse(lease)
	if err != nil {
		return nil, fail(err)
	}
	max := int(m.GetMaxMatches())
	if max <= 0 {
		max = 100
	}
	ms, truncated, err := br.Grep(ctx, lease.EnvID, m.GetPattern(), m.GetPath(), max)
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.GrepLeaseResponse{Matches: grepMatches(ms), Truncated: truncated}), nil
}

func archiveOf(lease *broker.Lease) (sandbox.Archive, error) {
	a, ok := lease.Box.(sandbox.Archive)
	if !ok {
		return nil, providererr.New(providererr.Invalid, "unsupported_archive", "这个沙箱后端不能导出、导入工作区")
	}
	return a, nil
}

func (h *handler) Manifest(ctx context.Context, req *connect.Request[sandboxv1.ManifestRequest]) (*connect.Response[sandboxv1.ManifestResponse], error) {
	m := req.Msg
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	a, err := archiveOf(lease)
	if err != nil {
		return nil, fail(err)
	}
	entries, err := a.Manifest(ctx, lease.EnvID)
	if err != nil {
		return nil, fail(err)
	}
	out := &sandboxv1.ManifestResponse{}
	for _, e := range entries {
		out.Entries = append(out.Entries, &sandboxv1.ManifestEntry{Path: e.Path, Type: e.Type, Mode: int32(e.Mode), Size: e.Size, Sha256: e.SHA256, Link: e.Link})
	}
	return connect.NewResponse(out), nil
}

const chunk = 1 << 20

func (h *handler) ExportArchive(ctx context.Context, req *connect.Request[sandboxv1.ExportArchiveRequest], stream *connect.ServerStream[sandboxv1.ExportArchiveResponse]) error {
	m := req.Msg
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return fail(err)
	}
	a, err := archiveOf(lease)
	if err != nil {
		return fail(err)
	}
	pr, pw := io.Pipe()
	done := make(chan error, 1)
	go func() {
		_, err := a.ExportArchive(ctx, lease.EnvID, pw, m.GetMaxBytes())
		_ = pw.CloseWithError(err)
		done <- err
	}()
	buf := make([]byte, chunk)
	for {
		n, rerr := io.ReadFull(pr, buf)
		if n > 0 {
			if err := stream.Send(&sandboxv1.ExportArchiveResponse{Data: append([]byte(nil), buf[:n]...)}); err != nil {
				_ = pr.CloseWithError(err)
				<-done
				return err
			}
		}
		if rerr != nil {
			if errors.Is(rerr, io.EOF) || errors.Is(rerr, io.ErrUnexpectedEOF) {
				break
			}
			<-done
			return fail(rerr)
		}
	}
	if err := <-done; err != nil {
		return fail(err)
	}
	return nil
}

func (h *handler) ImportArchive(ctx context.Context, stream *connect.ClientStream[sandboxv1.ImportArchiveRequest]) (*connect.Response[sandboxv1.ImportArchiveResponse], error) {
	if !stream.Receive() {
		return nil, fail(providererr.New(providererr.Invalid, "invalid_argument", "第一条消息要是 header"))
	}
	header := stream.Msg().GetHeader()
	if header == nil {
		return nil, fail(providererr.New(providererr.Invalid, "invalid_argument", "第一条消息要是 header"))
	}
	lease, err := h.lease(ctx, header.GetTenant(), header.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	a, err := archiveOf(lease)
	if err != nil {
		return nil, fail(err)
	}
	pr, pw := io.Pipe()
	go func() {
		var sent int64
		for stream.Receive() {
			data := stream.Msg().GetChunk()
			sent += int64(len(data))
			if sent > header.GetSize() {
				_ = pw.CloseWithError(providererr.New(providererr.Invalid, "too_large", "导入的存档比声明的大"))
				return
			}
			if _, err := pw.Write(data); err != nil {
				return
			}
		}
		_ = pw.CloseWithError(stream.Err())
	}()
	err = a.ImportArchive(ctx, lease.EnvID, pr, header.GetSize(), header.GetMaxBytes())
	_ = pr.Close()
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.ImportArchiveResponse{}), nil
}

func (h *handler) PreviewAddress(ctx context.Context, req *connect.Request[sandboxv1.PreviewAddressRequest]) (*connect.Response[sandboxv1.PreviewAddressResponse], error) {
	m := req.Msg
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	p, ok := lease.Box.(sandbox.Preview)
	if !ok {
		return nil, fail(providererr.New(providererr.Invalid, "preview_unsupported", "这个沙箱后端不能开预览地址"))
	}
	addr, err := p.PreviewAddress(ctx, lease.EnvID, int(m.GetPort()))
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.PreviewAddressResponse{Address: addr}), nil
}

func (h *handler) Release(ctx context.Context, req *connect.Request[sandboxv1.ReleaseRequest]) (*connect.Response[sandboxv1.ReleaseResponse], error) {
	m := req.Msg
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	if err := h.b().Release(ctx, lease, m.GetKeep(), m.GetCpuTimeMs(), m.GetSkipSnapshot()); err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.ReleaseResponse{Lease: h.s.leaseProto(lease)}), nil
}

func (h *handler) Snapshot(ctx context.Context, req *connect.Request[sandboxv1.SnapshotRequest]) (*connect.Response[sandboxv1.SnapshotResponse], error) {
	m := req.Msg
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	snap, err := h.b().Snapshot(ctx, lease)
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.SnapshotResponse{SnapshotId: snap.SnapshotID, Bytes: snap.SizeBytes, Files: int32(snap.Files)}), nil
}

func (h *handler) Discard(ctx context.Context, req *connect.Request[sandboxv1.DiscardRequest]) (*connect.Response[sandboxv1.DiscardResponse], error) {
	m := req.Msg
	lease, err := h.lease(ctx, m.GetTenant(), m.GetLeaseId())
	if err != nil {
		return nil, fail(err)
	}
	if err := h.b().Discard(ctx, lease, m.GetReason()); err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.DiscardResponse{}), nil
}

func (h *handler) DestroySession(ctx context.Context, req *connect.Request[sandboxv1.DestroySessionRequest]) (*connect.Response[sandboxv1.DestroySessionResponse], error) {
	t, err := tenant(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	n, err := h.b().DestroySession(ctx, t.GetTenantId(), req.Msg.GetWorkspaceId(), req.Msg.GetSessionId())
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.DestroySessionResponse{Destroyed: int32(n)}), nil
}

func (h *handler) DestroyWorkspace(ctx context.Context, req *connect.Request[sandboxv1.DestroyWorkspaceRequest]) (*connect.Response[sandboxv1.DestroyWorkspaceResponse], error) {
	t, err := tenant(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	n, err := h.b().DestroyWorkspace(ctx, t.GetTenantId(), req.Msg.GetWorkspaceId())
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.DestroyWorkspaceResponse{Destroyed: int32(n)}), nil
}

func (h *handler) DestroyTenant(ctx context.Context, req *connect.Request[sandboxv1.DestroyTenantRequest]) (*connect.Response[sandboxv1.DestroyTenantResponse], error) {
	t, err := tenant(req.Msg.GetTenant())
	if err != nil {
		return nil, fail(err)
	}
	n, err := h.b().DestroyTenant(ctx, t.GetTenantId())
	if err != nil {
		return nil, fail(err)
	}
	return connect.NewResponse(&sandboxv1.DestroyTenantResponse{Destroyed: int32(n)}), nil
}

// --- 转换 ---

func (s *Service) leaseProto(l *broker.Lease) *sandboxv1.Lease {
	out := &sandboxv1.Lease{
		LeaseId: l.ID(), Backend: &sandboxv1.Backend{Name: l.Backend, Kind: l.Kind, Isolation: l.Isolation}, BackendEnvId: l.EnvID, Reused: l.Reused,
		FallbackFrom: l.FallbackFrom, FallbackReason: l.FallbackReason, Network: l.Network, TestEnvNames: l.TestEnvNames, Archive: l.Archive,
		Writable: l.Writable, RestoredSnapshot: l.Restored, EnvironmentJson: l.EnvironmentJSON,
	}
	if l.Prepared != nil {
		out.Prepared = &sandboxv1.Prepared{EnvironmentId: l.Prepared.EnvironmentID, Setup: l.Prepared.Setup, Start: l.Prepared.Start}
	}
	if l.Record != nil {
		out.Record = RecordProto(*l.Record, s.opts.Clock.Now())
		out.CostUsd = l.Record.CostUSD
	}
	return out
}

func ts(s string) *timestamppb.Timestamp {
	t := ledger.ParseISO(s)
	if t.IsZero() {
		return nil
	}
	return timestamppb.New(t)
}

var states = map[string]environmentv1.EnvironmentState{
	"creating": environmentv1.EnvironmentState_ENVIRONMENT_STATE_CREATING, "ready": environmentv1.EnvironmentState_ENVIRONMENT_STATE_READY,
	"destroying": environmentv1.EnvironmentState_ENVIRONMENT_STATE_DESTROYING, "destroyed": environmentv1.EnvironmentState_ENVIRONMENT_STATE_DESTROYED,
	"failed": environmentv1.EnvironmentState_ENVIRONMENT_STATE_FAILED, "lost": environmentv1.EnvironmentState_ENVIRONMENT_STATE_LOST,
}

var setupStates = map[string]environmentv1.SetupState{
	"none": environmentv1.SetupState_SETUP_STATE_NONE, "running": environmentv1.SetupState_SETUP_STATE_RUNNING,
	"succeeded": environmentv1.SetupState_SETUP_STATE_SUCCEEDED, "cached": environmentv1.SetupState_SETUP_STATE_CACHED,
	"failed": environmentv1.SetupState_SETUP_STATE_FAILED,
}

var startStates = map[string]environmentv1.StartState{
	"none": environmentv1.StartState_START_STATE_NONE, "succeeded": environmentv1.StartState_START_STATE_SUCCEEDED,
	"failed": environmentv1.StartState_START_STATE_FAILED,
}

// RecordProto 把环境表的一行投影成 EnvRecord（同 EnvironmentService 的规则：ready 只在健康已确认、没到期、setup 成功时为 true）。
func RecordProto(r ledger.Record, now time.Time) *environmentv1.EnvRecord {
	n := ledger.ISO(now)
	setupOK := r.SetupState == "none" || r.SetupState == "succeeded" || r.SetupState == "cached"
	return &environmentv1.EnvRecord{
		EnvRef: r.EnvRef, TenantId: r.Owner, WorkspaceId: r.WorkspaceID, SessionId: r.SessionID, Backend: r.Backend, BackendEnvId: r.BackendEnvID,
		Kind: r.Kind, Isolation: r.Isolation, State: states[r.State], CreatedAt: ts(r.CreatedAt), ReadyAt: ts(r.ReadyAt), LastUsedAt: ts(r.LastUsedAt),
		ExpiresAt: ts(r.ExpiresAt), MaxUntil: ts(r.MaxUntil), DestroyedAt: ts(r.DestroyedAt), HealthConfirmedAt: ts(r.HealthConfirmedAt),
		ReusedAt: ts(r.ReusedAt),
		Ready:    r.State == "ready" && r.HealthConfirmedAt != "" && r.ExpiresAt > n && r.MaxUntil > n && setupOK,
		Reused:   r.ReusedAt != "" && r.WorkspaceID != "" && r.SessionID != "",
		TaskId:   r.TaskID, Writable: r.Writable, Runtime: r.Runtime, Image: r.Image, EnvironmentId: r.EnvironmentID, SetupState: setupStates[r.SetupState],
		SetupKey: r.SetupKey, SetupFinishedAt: ts(r.SetupFinishedAt), SetupError: r.SetupError, StartState: startStates[r.StartState],
	}
}
