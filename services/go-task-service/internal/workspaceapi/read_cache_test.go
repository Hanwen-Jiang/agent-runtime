package workspaceapi

// WS-READ-03 验收（docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：只读操作按工作区的版本缓存。
//   - 列目录、读文件、提交记录、diff 按（租户、工作区、generation、操作与参数）缓存：同样的请求第二次不再起工作流；
//     Worker 改动工作区后 generation 加一（py-worker 的 test_workspace_generation.py），旧的缓存不再命中。
//   - 写操作、出错的结果、太大的结果都不缓存；业务库里没有这个工作区的行（或没配 Cache）时不缓存。
//   - 缓存按租户隔开：别的租户同名的工作区不会读到这个租户的结果。
//   - 有 TTL 兜底（工具之外改了工作区、generation 没变时，最多旧这么久）。

import (
	"context"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
)

func cacheHarness(t *testing.T, clk *testkit.FakeClock, extra ...func(*Deps)) *harness {
	t.Helper()
	h := newHarness(t, append([]func(*Deps){func(d *Deps) {
		d.Cache = memcache.New(memcache.Options{Clock: clk})
		d.ReadCacheTTL = time.Minute
	}}, extra...)...)
	if err := h.store.Migrate(context.Background(), []relational.Migration{{Version: 205, Name: "workspaces", Statements: []string{
		`CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
			generation BIGINT NOT NULL DEFAULT 0)`,
	}}}); err != nil {
		t.Fatal(err)
	}
	// 每次读都给出不同的内容，看得出是缓存的还是新起的工作流。
	n := 0
	h.w.handle = func(in *workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		n++
		var res map[string]any
		switch in.GetOperation() {
		case workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_READ_FILE:
			res = map[string]any{"size": "1", "content": in.GetTenant().GetTenantId() + ":" + in.GetArgs().AsMap()["path"].(string) + ":" + string(rune('0'+n))}
		case workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LIST_FILES:
			res = map[string]any{"entries": []any{map[string]any{"name": string(rune('a' + n)), "type": "FILE_TYPE_FILE"}}}
		case workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LOG:
			res = map[string]any{"commits": []any{map[string]any{"sha": strings.Repeat("a", n), "message": "m"}}}
		case workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_DIFF:
			res = map[string]any{"diff": strings.Repeat("+", n), "head": "h"}
		default:
			res = map[string]any{"committed": true, "sha": "s"}
		}
		st, _ := structpb.NewStruct(res)
		return &workflowv1.WorkspaceOperationResult{Result: st}, nil
	}
	return h
}

func (h *harness) setGeneration(t *testing.T, tenant, workspace string, gen int64) {
	t.Helper()
	db, _ := h.store.Bind(&commonv1.TenantContext{TenantId: tenant})
	err := db.Transaction(context.Background(), relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `INSERT INTO workspaces (workspace_id, state, created_at, updated_at, generation) VALUES ($1, 'active', 'x', 'x', $2)
			ON CONFLICT (workspace_id) DO UPDATE SET generation = excluded.generation`, workspace, gen)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
}

func (h *harness) starts() int {
	h.w.mu.Lock()
	defer h.w.mu.Unlock()
	return len(h.w.started)
}

func (h *harness) read(t *testing.T, tenant, workspace, path string) string {
	t.Helper()
	resp, err := h.client.ReadFile(context.Background(), authed(h, tenant, &workspacev1.ReadFileRequest{WorkspaceId: workspace, Path: path}))
	if err != nil {
		t.Fatal(err)
	}
	return resp.Msg.GetContent()
}

func TestReadsAreCachedPerGeneration(t *testing.T) {
	clk := testkit.NewFakeClock()
	h := cacheHarness(t, clk)
	h.setGeneration(t, "t1", "w1", 1)
	first := h.read(t, "t1", "w1", "a.py")
	if again := h.read(t, "t1", "w1", "a.py"); again != first || h.starts() != 1 {
		t.Fatalf("同一版本再读要命中缓存：%q %q，起了 %d 个工作流", first, again, h.starts())
	}
	if other := h.read(t, "t1", "w1", "b.py"); other == first || h.starts() != 2 {
		t.Fatal("参数不同不能命中")
	}
	h.setGeneration(t, "t1", "w1", 2)
	if fresh := h.read(t, "t1", "w1", "a.py"); fresh == first || h.starts() != 3 {
		t.Fatal("generation 变了不能命中旧的缓存")
	}
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if _, err := h.client.ListFiles(ctx, authed(h, "t1", &workspacev1.ListFilesRequest{WorkspaceId: "w1", Path: "src"})); err != nil {
			t.Fatal(err)
		}
		if _, err := h.client.GetLog(ctx, authed(h, "t1", &workspacev1.GetLogRequest{WorkspaceId: "w1"})); err != nil {
			t.Fatal(err)
		}
		if _, err := h.client.GetDiff(ctx, authed(h, "t1", &workspacev1.GetDiffRequest{WorkspaceId: "w1"})); err != nil {
			t.Fatal(err)
		}
	}
	if h.starts() != 6 {
		t.Fatalf("列目录、提交记录、diff 各起一次，实际共 %d 个工作流", h.starts())
	}
}

func TestWritesAreNeverCached(t *testing.T) {
	h := cacheHarness(t, testkit.NewFakeClock())
	h.setGeneration(t, "t1", "w1", 1)
	for i := 0; i < 2; i++ {
		if _, err := h.client.CommitWorkspace(context.Background(), authed(h, "t1", &workspacev1.CommitWorkspaceRequest{WorkspaceId: "w1", Message: "m"})); err != nil {
			t.Fatal(err)
		}
	}
	if h.starts() != 2 {
		t.Fatalf("写操作每次都要起工作流，实际 %d", h.starts())
	}
}

func TestWithoutARowOrACacheNothingIsCached(t *testing.T) {
	h := cacheHarness(t, testkit.NewFakeClock())
	h.read(t, "t1", "loose", "a.py")
	h.read(t, "t1", "loose", "a.py")
	if h.starts() != 2 {
		t.Fatalf("业务库里没有这个工作区的行时不缓存，实际起了 %d 个", h.starts())
	}
	plain := newHarness(t)
	for i := 0; i < 2; i++ {
		if _, err := plain.client.ReadFile(context.Background(), authed(plain, "t1", &workspacev1.ReadFileRequest{WorkspaceId: "w1", Path: "a.py"})); err != nil {
			t.Fatal(err)
		}
	}
	if plain.starts() != 2 {
		t.Fatal("没配 Cache 时照旧每次起工作流")
	}
}

func TestErrorsAndLargeResultsAreNotCached(t *testing.T) {
	h := cacheHarness(t, testkit.NewFakeClock())
	h.setGeneration(t, "t1", "w1", 1)
	fail := true
	h.w.handle = func(in *workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		if fail {
			return &workflowv1.WorkspaceOperationResult{Error: providererr.New(providererr.NotFound, "file_not_found", "x").Proto()}, nil
		}
		st, _ := structpb.NewStruct(map[string]any{"size": "1", "content": strings.Repeat("x", maxCachedResult+1)})
		return &workflowv1.WorkspaceOperationResult{Result: st}, nil
	}
	for i := 0; i < 2; i++ {
		if _, err := h.client.ReadFile(context.Background(), authed(h, "t1", &workspacev1.ReadFileRequest{WorkspaceId: "w1", Path: "a.py"})); err == nil {
			t.Fatal("要报错")
		}
	}
	fail = false
	h.read(t, "t1", "w1", "a.py")
	h.read(t, "t1", "w1", "a.py")
	if h.starts() != 4 {
		t.Fatalf("出错与太大的结果都不缓存，实际起了 %d 个", h.starts())
	}
}

func TestCacheIsPerTenant(t *testing.T) {
	h := cacheHarness(t, testkit.NewFakeClock())
	h.setGeneration(t, "t1", "w1", 1)
	h.setGeneration(t, "t2", "w1", 1)
	a := h.read(t, "t1", "w1", "a.py")
	b := h.read(t, "t2", "w1", "a.py")
	if !strings.HasPrefix(a, "t1:") || !strings.HasPrefix(b, "t2:") || h.starts() != 2 {
		t.Fatalf("别的租户同名的工作区不能读到这个租户的缓存：%q %q", a, b)
	}
}

func TestCachedReadsExpire(t *testing.T) {
	clk := testkit.NewFakeClock()
	h := cacheHarness(t, clk)
	h.setGeneration(t, "t1", "w1", 1)
	h.read(t, "t1", "w1", "a.py")
	clk.Advance(2 * time.Minute)
	h.read(t, "t1", "w1", "a.py")
	if h.starts() != 2 {
		t.Fatal("过了 TTL 要重新读")
	}
}
