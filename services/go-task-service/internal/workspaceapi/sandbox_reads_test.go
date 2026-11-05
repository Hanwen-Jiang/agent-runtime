package workspaceapi

// WS-READ-01（SBX-01）：列目录、读文件、提交记录、diff 直接调 Sandbox Service 的只读方法，不起 Worker 的工作流。
//   - 传给服务的租户是服务端按认证身份定的（不是请求里写的），工作区与参数原样带过去；服务的结果原样回给调用方。
//   - WS-READ-03 的缓存照旧：同一版本再读不再调服务。
//   - 服务的错误（工作区不在、路径不对）按统一错误模型回给调用方；写操作仍起工作流。
//   - 没配服务时照旧起工作流（开发时 task-service 可以不连服务）。

import (
	"context"
	"sync"
	"testing"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
)

type fakeReader struct {
	mu    sync.Mutex
	calls []string
	seen  []any
	fail  error
}

func (f *fakeReader) record(method string, req any) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, method)
	f.seen = append(f.seen, req)
	return f.fail
}

func (f *fakeReader) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

func (f *fakeReader) ListFiles(_ context.Context, r *connect.Request[sandboxv1.ListFilesRequest]) (*connect.Response[sandboxv1.ListFilesResponse], error) {
	if err := f.record("ListFiles", r.Msg); err != nil {
		return nil, err
	}
	return connect.NewResponse(&sandboxv1.ListFilesResponse{Response: &workspacev1.ListFilesResponse{Entries: []*workspacev1.FileEntry{{Name: "src", Type: workspacev1.FileType_FILE_TYPE_DIRECTORY}}}}), nil
}

func (f *fakeReader) ReadFile(_ context.Context, r *connect.Request[sandboxv1.ReadFileRequest]) (*connect.Response[sandboxv1.ReadFileResponse], error) {
	if err := f.record("ReadFile", r.Msg); err != nil {
		return nil, err
	}
	return connect.NewResponse(&sandboxv1.ReadFileResponse{Response: &workspacev1.ReadFileResponse{Content: r.Msg.GetTenant().GetTenantId() + ":" + r.Msg.GetPath(), Size: 3}}), nil
}

func (f *fakeReader) GetDiff(_ context.Context, r *connect.Request[sandboxv1.GetDiffRequest]) (*connect.Response[sandboxv1.GetDiffResponse], error) {
	if err := f.record("GetDiff", r.Msg); err != nil {
		return nil, err
	}
	return connect.NewResponse(&sandboxv1.GetDiffResponse{Response: &workspacev1.GetDiffResponse{Diff: "+x", Files: []string{"a.py"}}}), nil
}

func (f *fakeReader) GetLog(_ context.Context, r *connect.Request[sandboxv1.GetLogRequest]) (*connect.Response[sandboxv1.GetLogResponse], error) {
	if err := f.record("GetLog", r.Msg); err != nil {
		return nil, err
	}
	return connect.NewResponse(&sandboxv1.GetLogResponse{Response: &workspacev1.GetLogResponse{Commits: []*workspacev1.Commit{{Sha: "abc", Subject: "m"}}}}), nil
}

func readerHarness(t *testing.T) (*harness, *fakeReader) {
	t.Helper()
	f := &fakeReader{}
	h := cacheHarness(t, testkit.NewFakeClock(), func(d *Deps) { d.Sandbox = f })
	return h, f
}

func TestReadsGoToTheSandboxServiceWithTheServerSideTenant(t *testing.T) {
	h, f := readerHarness(t)
	ctx := context.Background()
	read, err := h.client.ReadFile(ctx, authed(h, "t1", &workspacev1.ReadFileRequest{WorkspaceId: "w1", Path: "src/a.py"}))
	if err != nil {
		t.Fatal(err)
	}
	if read.Msg.GetContent() != "t1:src/a.py" {
		t.Fatalf("结果原样转回：%v", read.Msg)
	}
	if _, err := h.client.ListFiles(ctx, authed(h, "t1", &workspacev1.ListFilesRequest{WorkspaceId: "w1", Path: "src"})); err != nil {
		t.Fatal(err)
	}
	diff, err := h.client.GetDiff(ctx, authed(h, "t1", &workspacev1.GetDiffRequest{WorkspaceId: "w1", Base: "main", FindRenames: true}))
	if err != nil || diff.Msg.GetDiff() != "+x" {
		t.Fatalf("%v %v", diff, err)
	}
	logs, err := h.client.GetLog(ctx, authed(h, "t1", &workspacev1.GetLogRequest{WorkspaceId: "w1", Limit: 5, Base: "main"}))
	if err != nil || logs.Msg.GetCommits()[0].GetSha() != "abc" {
		t.Fatalf("%v %v", logs, err)
	}
	if h.starts() != 0 {
		t.Fatalf("只读的不起 Worker 的工作流，实际起了 %d 个", h.starts())
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.seen) != 4 {
		t.Fatalf("%v", f.calls)
	}
	r := f.seen[0].(*sandboxv1.ReadFileRequest)
	if r.GetTenant().GetTenantId() != "t1" || r.GetTenant().GetUserId() == "" || r.GetWorkspaceId() != "w1" || r.GetPath() != "src/a.py" {
		t.Fatalf("租户按认证身份、工作区与路径原样：%v", r)
	}
	if l := f.seen[1].(*sandboxv1.ListFilesRequest); l.GetPath() != "src" || l.GetWorkspaceId() != "w1" {
		t.Fatalf("%v", l)
	}
	if d := f.seen[2].(*sandboxv1.GetDiffRequest); d.GetBase() != "main" || !d.GetFindRenames() {
		t.Fatalf("%v", d)
	}
	if g := f.seen[3].(*sandboxv1.GetLogRequest); g.GetLimit() != 5 || g.GetBase() != "main" {
		t.Fatalf("%v", g)
	}
}

func TestSandboxReadsKeepTheGenerationCache(t *testing.T) {
	h, f := readerHarness(t)
	h.setGeneration(t, "t1", "w1", 1)
	h.read(t, "t1", "w1", "a.py")
	h.read(t, "t1", "w1", "a.py")
	if f.count() != 1 {
		t.Fatalf("同一版本再读要命中缓存，服务被调了 %d 次", f.count())
	}
	h.setGeneration(t, "t1", "w1", 2)
	h.read(t, "t1", "w1", "a.py")
	if f.count() != 2 {
		t.Fatal("generation 变了要重新读")
	}
}

func TestSandboxErrorsReachTheCallerAndWritesStillStartWorkflows(t *testing.T) {
	h, f := readerHarness(t)
	f.fail = connecterr.Error(providererr.New(providererr.NotFound, "workspace_not_found", "没有这个工作区"))
	_, err := h.client.ReadFile(context.Background(), authed(h, "t1", &workspacev1.ReadFileRequest{WorkspaceId: "nope", Path: "a.py"}))
	if c, reason := reasonOf(t, err); c != connect.CodeNotFound || reason != "workspace_not_found" {
		t.Fatalf("服务的错误按统一错误模型回给调用方：%v", err)
	}
	if _, err := h.client.CommitWorkspace(context.Background(), authed(h, "t1", &workspacev1.CommitWorkspaceRequest{WorkspaceId: "w1", Message: "m"})); err != nil {
		t.Fatal(err)
	}
	if h.starts() != 1 {
		t.Fatalf("写操作仍起工作流：%d", h.starts())
	}
}
