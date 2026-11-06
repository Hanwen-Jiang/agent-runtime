package workspaceapi

// D-06e 的 task-service 一侧（docs/plan/workspace-layer.md 第 5 节）：认证与租户、参数校验、凭据封装、起工作流并按结果作答、
// ListWorkspaces 读业务库。Worker 一侧的操作由 services/py-worker 的 test_workspace_operations.py 测，端到端在 tests/e2e。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/sealing"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1/workspacev1connect"
)

var key = []byte(strings.Repeat("k", 32))

// worker 是 Durable 的替身：记下起的工作流，用 handle 模拟 Worker 给出结果。
type worker struct {
	mu          sync.Mutex
	started     []*workflowv1.WorkspaceOperationInput
	scopes      []string
	handle      func(in *workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error)
	status      durable.Status
	beforeStart func(context.Context) error
}

func (w *worker) Bind(t *commonv1.TenantContext) (durable.Engine, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &engine{w: w, scope: s.TenantID}, nil
}
func (w *worker) BindPlatform(string) (durable.Engine, error) {
	return nil, providererr.New(providererr.Denied, "x", "x")
}
func (w *worker) Close() error { return nil }

type engine struct {
	durable.Engine
	w     *worker
	scope string
	last  *workflowv1.WorkspaceOperationInput
}

func (e *engine) StartWorkflow(ctx context.Context, typ string, opts durable.StartOptions) (durable.Run, error) {
	// WS-READ-02：新起的操作放 workspace 队列。
	if typ != WorkflowName || opts.TaskQueue != "workspace" || !strings.HasPrefix(opts.WorkflowID, "wsop-wso-") {
		return durable.Run{}, providererr.New(providererr.Invalid, "unexpected", typ+" "+opts.TaskQueue+" "+opts.WorkflowID)
	}
	if e.w.beforeStart != nil {
		if err := e.w.beforeStart(ctx); err != nil {
			return durable.Run{}, err
		}
	}
	in := proto.Clone(opts.Input).(*workflowv1.WorkspaceOperationInput)
	e.w.mu.Lock()
	e.w.started = append(e.w.started, in)
	e.w.scopes = append(e.w.scopes, e.scope)
	e.w.mu.Unlock()
	e.last = in
	return durable.Run{WorkflowID: opts.WorkflowID}, nil
}

func (e *engine) Result(_ context.Context, id string, result proto.Message, _ time.Duration) (durable.Outcome, error) {
	if e.last == nil {
		e.w.mu.Lock()
		for i, in := range e.w.started {
			if WorkflowID(in.OperationId) == id && e.w.scopes[i] == e.scope {
				e.last = in
				break
			}
		}
		e.w.mu.Unlock()
		if e.last == nil {
			return durable.Outcome{}, providererr.New(providererr.NotFound, "not_found", "missing workflow")
		}
	}
	out, err := e.w.handle(e.last)
	if err != nil {
		return durable.Outcome{}, err
	}
	proto.Merge(result, out)
	return durable.Outcome{Status: durable.StatusCompleted}, nil
}

func (e *engine) Describe(_ context.Context, id string) (durable.Description, error) {
	e.w.mu.Lock()
	defer e.w.mu.Unlock()
	for i, in := range e.w.started {
		if WorkflowID(in.OperationId) == id && e.w.scopes[i] == e.scope {
			status := e.w.status
			if status == "" {
				status = durable.StatusCompleted
			}
			return durable.Description{WorkflowID: id, WorkflowType: WorkflowName, Status: status}, nil
		}
	}
	return durable.Description{}, providererr.New(providererr.NotFound, "not_found", "missing workflow")
}

type harness struct {
	client workspacev1connect.WorkspaceServiceClient
	ing    *localingress.Provider
	w      *worker
	store  relational.Provider
}

func newHarness(t *testing.T, opts ...func(*Deps)) *harness {
	t.Helper()
	ing := localingress.New(localingress.Options{SigningKey: []byte(strings.Repeat("s", 32)), Limit: 1000, Window: time.Minute})
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	w := &worker{handle: func(*workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		return &workflowv1.WorkspaceOperationResult{Result: &structpb.Struct{}}, nil
	}}
	d := Deps{Ingress: ing, Durable: w, Store: store, SealingKey: func(context.Context) ([]byte, error) { return key, nil }}
	for _, o := range opts {
		o(&d)
	}
	srv, err := New(d)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	srv.Mount(mux)
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	return &harness{client: workspacev1connect.NewWorkspaceServiceClient(ts.Client(), ts.URL), ing: ing, w: w, store: store}
}

func authed[T any](h *harness, tenant string, msg *T) *connect.Request[T] {
	req := connect.NewRequest(msg)
	req.Header().Set("Authorization", "Bearer "+h.ing.IssueToken(tenant, "u-alice", time.Hour))
	return req
}

func code(t *testing.T, err error) connect.Code {
	t.Helper()
	if err == nil {
		t.Fatal("要报错")
	}
	return connect.CodeOf(err)
}

func TestCreateSealsTheCredentialAndStartsTheWorkerWorkflow(t *testing.T) {
	h := newHarness(t)
	h.w.handle = func(in *workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		res, _ := structpb.NewStruct(map[string]any{"workspace": map[string]any{
			"workspace_id": in.GetWorkspaceId(), "state": "WORKSPACE_STATE_ACTIVE", "repo_url": in.GetArgs().AsMap()["repo_url"], "head": "abc123",
			"size_bytes": "42", "files": 3, "created_at": "2026-10-27T10:00:00.000000Z", "extra": "ignored",
		}})
		return &workflowv1.WorkspaceOperationResult{Result: res}, nil
	}
	resp, err := h.client.CreateWorkspace(context.Background(), authed(h, "t1", &workspacev1.CreateWorkspaceRequest{
		TenantId: "t1", WorkspaceId: "w1", RepoUrl: "https://git.example.com/acme/refund.git", Ref: "main",
		Credential: &workspacev1.GitCredential{Username: "bot", Password: "pw-SECRET-123"},
	}))
	if err != nil {
		t.Fatal(err)
	}
	ws := resp.Msg.GetWorkspace()
	if ws.GetWorkspaceId() != "w1" || ws.GetHead() != "abc123" || ws.GetSizeBytes() != 42 || ws.GetFiles() != 3 || ws.GetCreatedAt().AsTime().Year() != 2026 {
		t.Fatalf("结果没有原样带回：%v", ws)
	}
	in := h.w.started[0]
	if h.w.scopes[0] != "t1" || in.GetTenant().GetTenantId() != "t1" || in.GetRequestedBy() != "u-alice" || in.GetOperation() != workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_CREATE {
		t.Fatalf("工作流的输入不对：%v", in)
	}
	raw, _ := protojson.Marshal(in)
	if strings.Contains(string(raw), "pw-SECRET-123") {
		t.Fatal("凭据的明文进了工作流的输入")
	}
	plain, err := sealing.Open(key, "t1", in.GetOperationId(), in.GetSealedCredential())
	if err != nil {
		t.Fatal(err)
	}
	var cred map[string]string
	_ = json.Unmarshal(plain, &cred)
	if cred["username"] != "bot" || cred["password"] != "pw-SECRET-123" {
		t.Fatalf("封进去的凭据不对：%v", cred)
	}
	if _, err := sealing.Open(key, "t2", in.GetOperationId(), in.GetSealedCredential()); err == nil {
		t.Fatal("封给 t1 的凭据 t2 解不开")
	}
}

func TestTheTenantComesFromTheToken(t *testing.T) {
	h := newHarness(t)
	_, err := h.client.GetDiff(context.Background(), authed(h, "t1", &workspacev1.GetDiffRequest{TenantId: "t2", WorkspaceId: "w1"}))
	if code(t, err) != connect.CodePermissionDenied {
		t.Fatalf("请求里的 tenant_id 与身份不一致：%v", err)
	}
	_, err = h.client.GetDiff(context.Background(), connect.NewRequest(&workspacev1.GetDiffRequest{WorkspaceId: "w1"}))
	if code(t, err) != connect.CodeUnauthenticated {
		t.Fatalf("没有令牌：%v", err)
	}
	if len(h.w.started) != 0 {
		t.Fatal("被拒的请求不起工作流")
	}
}

func TestArgumentsAreCheckedBeforeStartingAnything(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	bad := []func() error{
		func() error {
			_, err := h.client.CreateWorkspace(ctx, authed(h, "t1", &workspacev1.CreateWorkspaceRequest{WorkspaceId: "W1"}))
			return err
		},
		func() error {
			_, err := h.client.CreateWorkspace(ctx, authed(h, "t1", &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w1", RepoUrl: "http://git.example.com/r.git"}))
			return err
		},
		func() error {
			_, err := h.client.CreateWorkspace(ctx, authed(h, "t1", &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w1", RepoUrl: "https://u:p@git.example.com/r.git"}))
			return err
		},
		func() error {
			_, err := h.client.CreateWorkspace(ctx, authed(h, "t1", &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w1", RepoUrl: "file:///tmp/r.git"}))
			return err
		},
		func() error {
			_, err := h.client.PushWorkspace(ctx, authed(h, "t1", &workspacev1.PushWorkspaceRequest{WorkspaceId: "w1", Branch: "--force"}))
			return err
		},
		func() error {
			_, err := h.client.PushWorkspace(ctx, authed(h, "t1", &workspacev1.PushWorkspaceRequest{WorkspaceId: "w1"}))
			return err
		},
		func() error {
			_, err := h.client.CommitWorkspace(ctx, authed(h, "t1", &workspacev1.CommitWorkspaceRequest{WorkspaceId: "w1", Message: "  "}))
			return err
		},
		func() error {
			_, err := h.client.GetDiff(ctx, authed(h, "t1", &workspacev1.GetDiffRequest{WorkspaceId: "w1", Base: "a..b"}))
			return err
		},
	}
	for i, f := range bad {
		if c := code(t, f()); c != connect.CodeInvalidArgument {
			t.Fatalf("第 %d 个：要 InvalidArgument，得到 %v", i, c)
		}
	}
	if len(h.w.started) != 0 {
		t.Fatal("参数不对的请求不起工作流")
	}
	local := newHarness(t, func(d *Deps) { d.AllowLocalRemotes = true })
	if _, err := local.client.CreateWorkspace(ctx, authed(local, "t1", &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w1", RepoUrl: "file:///tmp/r.git"})); err != nil {
		t.Fatalf("测试开关打开时收 file://：%v", err)
	}
}

func TestCredentialsNeedTheSealingKey(t *testing.T) {
	h := newHarness(t, func(d *Deps) { d.SealingKey = func(context.Context) ([]byte, error) { return nil, nil } })
	_, err := h.client.PushWorkspace(context.Background(), authed(h, "t1", &workspacev1.PushWorkspaceRequest{
		WorkspaceId: "w1", Branch: "agent/x", Credential: &workspacev1.GitCredential{Password: "pw-SECRET-123"},
	}))
	if code(t, err) != connect.CodeInvalidArgument || strings.Contains(err.Error(), "pw-SECRET-123") {
		t.Fatalf("没有封凭据的密钥：%v", err)
	}
	if _, err := h.client.PushWorkspace(context.Background(), authed(h, "t1", &workspacev1.PushWorkspaceRequest{WorkspaceId: "w1", Branch: "agent/x"})); err != nil {
		t.Fatalf("不带凭据时不要密钥：%v", err)
	}
}

func TestWorkerErrorsAndTimeoutsMapToConnectCodes(t *testing.T) {
	h := newHarness(t)
	cases := map[commonv1.ErrorCategory]connect.Code{
		commonv1.ErrorCategory_ERROR_CATEGORY_NOT_FOUND: connect.CodeNotFound,
		commonv1.ErrorCategory_ERROR_CATEGORY_CONFLICT:  connect.CodeAlreadyExists,
		commonv1.ErrorCategory_ERROR_CATEGORY_DENIED:    connect.CodePermissionDenied,
		commonv1.ErrorCategory_ERROR_CATEGORY_INVALID:   connect.CodeInvalidArgument,
		commonv1.ErrorCategory_ERROR_CATEGORY_TRANSIENT: connect.CodeUnavailable,
	}
	for category, want := range cases {
		h.w.handle = func(*workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
			return &workflowv1.WorkspaceOperationResult{Error: &commonv1.Error{Category: category, Code: "x", Message: "说明"}}, nil
		}
		_, err := h.client.CommitWorkspace(context.Background(), authed(h, "t1", &workspacev1.CommitWorkspaceRequest{WorkspaceId: "w1", Message: "m"}))
		if got := code(t, err); got != want {
			t.Fatalf("%v：要 %v，得到 %v", category, want, got)
		}
	}
	h.w.handle = func(*workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		return nil, providererr.New(providererr.Transient, "timeout", "等太久")
	}
	_, err := h.client.ArchiveWorkspace(context.Background(), authed(h, "t1", &workspacev1.ArchiveWorkspaceRequest{WorkspaceId: "w1"}))
	if code(t, err) != connect.CodeUnavailable {
		t.Fatalf("等结果超时：%v", err)
	}
}

func TestDiffResultIsCarriedBack(t *testing.T) {
	h := newHarness(t)
	h.w.handle = func(in *workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		if in.GetArgs().AsMap()["base"] != "v1.2" {
			t.Errorf("base 没有带给 Worker：%v", in.GetArgs())
		}
		res, _ := structpb.NewStruct(map[string]any{"diff": "--- a\n+++ b\n", "files": []any{"a.py", "b.py"}, "truncated": false, "head": "def456"})
		return &workflowv1.WorkspaceOperationResult{Result: res}, nil
	}
	resp, err := h.client.GetDiff(context.Background(), authed(h, "t1", &workspacev1.GetDiffRequest{WorkspaceId: "w1", Base: "v1.2"}))
	if err != nil {
		t.Fatal(err)
	}
	if resp.Msg.GetDiff() != "--- a\n+++ b\n" || len(resp.Msg.GetFiles()) != 2 || resp.Msg.GetHead() != "def456" {
		t.Fatalf("diff 没有原样带回：%v", resp.Msg)
	}
}

func TestListWorkspacesReadsTheBusinessDatabase(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	resp, err := h.client.ListWorkspaces(ctx, authed(h, "t1", &workspacev1.ListWorkspacesRequest{}))
	if err != nil || len(resp.Msg.GetWorkspaces()) != 0 {
		t.Fatalf("表还没有（Worker 还没迁移）时为空：%v %v", resp, err)
	}
	// 按 Worker 的迁移 205 与 201 建表（列只取这里用到的）。
	if err := h.store.Migrate(ctx, []relational.Migration{{Version: 205, Name: "workspaces", Statements: []string{
		`CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY, state TEXT NOT NULL, repo_url TEXT NOT NULL DEFAULT '', head TEXT NOT NULL DEFAULT '',
			snapshot_id TEXT NOT NULL DEFAULT '', size_bytes BIGINT NOT NULL DEFAULT 0, files BIGINT NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL, deleted_at TEXT NOT NULL DEFAULT '')`,
		`CREATE TABLE sandbox_envs (env_ref TEXT PRIMARY KEY, workspace_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL)`,
	}}}); err != nil {
		t.Fatal(err)
	}
	t1, _ := h.store.Bind(&commonv1.TenantContext{TenantId: "t1"})
	err = t1.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		for _, r := range [][]any{
			{"w-live", "active", "s1"}, {"w-cold", "active", "s2"}, {"w-new", "active", ""}, {"w-gone", "deleted", "s3"},
		} {
			if _, err := tx.Execute(ctx, `INSERT INTO workspaces (workspace_id, state, snapshot_id, size_bytes, files, created_at, updated_at)
				VALUES ($1, $2, $3, 10, 1, '2026-10-27T10:00:00.000000Z', '2026-10-27T11:00:00.000000Z')`, r...); err != nil {
				return err
			}
		}
		_, err := tx.Execute(ctx, `INSERT INTO sandbox_envs (env_ref, workspace_id, state) VALUES ('sbe-1', 'w-live', 'ready')`)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	resp, err = h.client.ListWorkspaces(ctx, authed(h, "t1", &workspacev1.ListWorkspacesRequest{TenantId: "t1"}))
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]workspacev1.WorkspaceState{}
	for _, w := range resp.Msg.GetWorkspaces() {
		got[w.GetWorkspaceId()] = w.GetState()
	}
	want := map[string]workspacev1.WorkspaceState{
		"w-live": workspacev1.WorkspaceState_WORKSPACE_STATE_ACTIVE, "w-cold": workspacev1.WorkspaceState_WORKSPACE_STATE_ARCHIVED,
		"w-new": workspacev1.WorkspaceState_WORKSPACE_STATE_ACTIVE,
	}
	if len(got) != len(want) || got["w-live"] != want["w-live"] || got["w-cold"] != want["w-cold"] || got["w-new"] != want["w-new"] {
		t.Fatalf("列出的工作区不对：%v", got)
	}
	other, err := h.client.ListWorkspaces(ctx, authed(h, "t2", &workspacev1.ListWorkspacesRequest{}))
	if err != nil || len(other.Msg.GetWorkspaces()) != 0 {
		t.Fatalf("别的租户看不到：%v %v", other, err)
	}
}

func TestBrowseAndManageCarryTheirArgumentsAndResults(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	results := map[workflowv1.WorkspaceOperation]map[string]any{
		workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LIST_FILES: {"entries": []any{map[string]any{"name": "src", "type": "FILE_TYPE_DIRECTORY"}}, "truncated": false},
		workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_READ_FILE:  {"size": "6", "content": "n = 1\n"},
		workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LOG: {"commits": []any{map[string]any{
			"sha": "abc", "author_name": "Alice", "author_email": "a@example.com", "authored_at": "2026-10-28T09:00:00+08:00", "subject": "fix"}}},
		workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_DISCARD: {"head": "abc"},
		workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_SYNC:    {"head": "def", "branch": "main"},
	}
	h.w.handle = func(in *workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		res, _ := structpb.NewStruct(results[in.GetOperation()])
		return &workflowv1.WorkspaceOperationResult{Result: res}, nil
	}
	files, err := h.client.ListFiles(ctx, authed(h, "t1", &workspacev1.ListFilesRequest{WorkspaceId: "w1", Path: "src"}))
	if err != nil || files.Msg.GetEntries()[0].GetType() != workspacev1.FileType_FILE_TYPE_DIRECTORY {
		t.Fatalf("ListFiles：%v %v", files, err)
	}
	read, err := h.client.ReadFile(ctx, authed(h, "t1", &workspacev1.ReadFileRequest{WorkspaceId: "w1", Path: "a.py"}))
	if err != nil || read.Msg.GetContent() != "n = 1\n" || read.Msg.GetSize() != 6 {
		t.Fatalf("ReadFile：%v %v", read, err)
	}
	logResp, err := h.client.GetLog(ctx, authed(h, "t1", &workspacev1.GetLogRequest{WorkspaceId: "w1", Limit: 5, Base: "origin/main"}))
	if err != nil || logResp.Msg.GetCommits()[0].GetAuthoredAt().AsTime().UTC().Hour() != 1 {
		t.Fatalf("GetLog（时间带时区）：%v %v", logResp, err)
	}
	if _, err := h.client.DiscardChanges(ctx, authed(h, "t1", &workspacev1.DiscardChangesRequest{WorkspaceId: "w1"})); err != nil {
		t.Fatal(err)
	}
	synced, err := h.client.SyncWorkspace(ctx, authed(h, "t1", &workspacev1.SyncWorkspaceRequest{
		WorkspaceId: "w1", Branch: "main", Credential: &workspacev1.GitCredential{Username: "bot", Password: "pw-SECRET-123"},
	}))
	if err != nil || synced.Msg.GetHead() != "def" {
		t.Fatalf("SyncWorkspace：%v %v", synced, err)
	}
	args := map[workflowv1.WorkspaceOperation]map[string]any{}
	for _, in := range h.w.started {
		args[in.GetOperation()] = in.GetArgs().AsMap()
	}
	if args[workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LIST_FILES]["path"] != "src" || args[workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LOG]["limit"] != float64(5) ||
		args[workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LOG]["base"] != "origin/main" || args[workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_SYNC]["branch"] != "main" {
		t.Fatalf("参数没有带给 Worker：%v", args)
	}
	if last := h.w.started[len(h.w.started)-1]; last.GetSealedCredential() == "" || strings.Contains(last.GetSealedCredential(), "pw-SECRET-123") {
		t.Fatal("同步的凭据要封起来")
	}
}

func TestCreateFromFilesOrAnotherWorkspaceIsChecked(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	files := []*workspacev1.InitialFile{{Path: "README.md", Content: "# 示例\n"}, {Path: "src/a.py", Content: "x = 1\n"}}
	if _, err := h.client.CreateWorkspace(ctx, authed(h, "t1", &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w1", Files: files, AuthorName: "Alice"})); err != nil {
		t.Fatal(err)
	}
	got := h.w.started[0].GetArgs().AsMap()
	if fs, _ := got["files"].([]any); len(fs) != 2 || got["author_name"] != "Alice" {
		t.Fatalf("初始文件与作者没有带给 Worker：%v", got)
	}
	if _, err := h.client.CreateWorkspace(ctx, authed(h, "t1", &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w2", FromWorkspaceId: "w1"})); err != nil {
		t.Fatal(err)
	}
	bad := []*workspacev1.CreateWorkspaceRequest{
		{WorkspaceId: "w3", FromWorkspaceId: "w1", Files: files},
		{WorkspaceId: "w3", RepoUrl: "https://git.example.com/r.git", FromWorkspaceId: "w1"},
		{WorkspaceId: "w3", FromWorkspaceId: "w3"},
		{WorkspaceId: "w3", FromWorkspaceId: "W1"},
		{WorkspaceId: "w3", Files: []*workspacev1.InitialFile{{Path: ".git/config", Content: "x"}}},
		{WorkspaceId: "w3", Files: []*workspacev1.InitialFile{{Path: "../x", Content: "x"}}},
		{WorkspaceId: "w3", Files: []*workspacev1.InitialFile{{Path: "big", Content: strings.Repeat("x", maxInitialBytes+1)}}},
		{WorkspaceId: "w3", AuthorEmail: "<evil>"},
	}
	for i, m := range bad {
		if _, err := h.client.CreateWorkspace(ctx, authed(h, "t1", m)); code(t, err) != connect.CodeInvalidArgument {
			t.Fatalf("第 %d 个要 InvalidArgument：%v", i, err)
		}
	}
	for _, p := range []string{".git", "a/../b", "/etc/passwd", "a\\b"} {
		if _, err := h.client.ReadFile(ctx, authed(h, "t1", &workspacev1.ReadFileRequest{WorkspaceId: "w1", Path: p})); code(t, err) != connect.CodeInvalidArgument {
			t.Fatalf("ReadFile %q 要 InvalidArgument：%v", p, err)
		}
	}
	if _, err := h.client.GetLog(ctx, authed(h, "t1", &workspacev1.GetLogRequest{WorkspaceId: "w1", Limit: 500})); code(t, err) != connect.CodeInvalidArgument {
		t.Fatalf("limit 太大：%v", err)
	}
	if len(h.w.started) != 2 {
		t.Fatalf("被拒的请求不起工作流：起了 %d 个", len(h.w.started))
	}
}

// TMP 计划 eval-observability-and-runtime-fixes 第 11.10 节（S-01 草案）：克隆到一个提交，并在克隆之后写入文件（SWE-bench 的
// base_commit 与题目的运行环境定义）。files 只与 from_workspace_id 互斥。
func TestCreateAtACommitWithFiles(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	commit := strings.Repeat("ab", 20)
	files := []*workspacev1.InitialFile{{Path: ".devcontainer/devcontainer.json", Content: "{}\n"}}
	req := &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w1", RepoUrl: "https://git.example.com/r.git", Ref: commit, Files: files}
	if _, err := h.client.CreateWorkspace(ctx, authed(h, "t1", req)); err != nil {
		t.Fatalf("repo_url、提交与 files 一起给：%v", err)
	}
	got := h.w.started[0].GetArgs().AsMap()
	if fs, _ := got["files"].([]any); got["ref"] != commit || got["repo_url"] != "https://git.example.com/r.git" || len(fs) != 1 {
		t.Fatalf("提交与文件没有带给 Worker：%v", got)
	}
	if _, err := h.client.CreateWorkspace(ctx, authed(h, "t1", &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w2", Ref: strings.Repeat("c", 64), RepoUrl: "https://git.example.com/r.git"})); err != nil {
		t.Fatalf("64 位的提交（SHA-256 仓库）：%v", err)
	}
	if _, err := h.client.CreateWorkspace(ctx, authed(h, "t1", &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w3", FromWorkspaceId: "w1", Files: files})); code(t, err) != connect.CodeInvalidArgument {
		t.Fatalf("files 与 from_workspace_id 仍然互斥：%v", err)
	}
	if len(h.w.started) != 2 {
		t.Fatalf("被拒的请求不起工作流：起了 %d 个", len(h.w.started))
	}
}

func TestWorkflowNameMatchesTheContract(t *testing.T) {
	v := workflowv1.File_agentruntime_workflow_v1_names_proto.Enums().ByName("WorkflowType").Values().ByName(protoreflect.Name("WORKFLOW_TYPE_WORKSPACE_OPERATION"))
	if v == nil {
		t.Fatal("names.proto 里没有 WORKFLOW_TYPE_WORKSPACE_OPERATION")
	}
	if name, _ := proto.GetExtension(v.Options(), commonv1.E_WireName).(string); name != WorkflowName {
		t.Fatalf("工作流名要等于契约里的 %q，实际 %q", name, WorkflowName)
	}
}

func TestTaskQueueMatchesTheContract(t *testing.T) {
	v := workflowv1.File_agentruntime_workflow_v1_names_proto.Enums().ByName("TaskQueue").Values().ByName(protoreflect.Name("TASK_QUEUE_WORKSPACE"))
	if v == nil {
		t.Fatal("names.proto 里没有 TASK_QUEUE_WORKSPACE")
	}
	if name, _ := proto.GetExtension(v.Options(), commonv1.E_WireName).(string); name != TaskQueue {
		t.Fatalf("队列名要等于契约里的 %q，实际 %q", name, TaskQueue)
	}
}
