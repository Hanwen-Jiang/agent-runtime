package server_test

// DISK-B（WS-DISK-01 ②）：挂卷工作区的冷热分层在 Sandbox Service 里（SBX-01 从 Worker 的 tests/test_workspace_cold.py 搬来）。
//
//  1. 闲置超过天数、没有环境占着的工作区：先存一份快照、与环境里的清单核对一致，才删本地目录；状态记成 cold。
//  2. 下次用到时从快照恢复到本地，内容与转冷前一致，状态回到 active（另见 TestColdWorkspaceIsRestoredWhenLeased）。
//  3. 最近用过的、还有环境占着的、存档模式的租户都不动。
//  4. 天数：租户策略的 cold_after_days 优先，没写时用部署的默认值，都没有时不转冷。
//  5. 存档失败时本地目录不删，状态回到 active。
//  6. 转冷进行中借这个工作区：暂时性的错误（workspace_cooling）。
//  7. 对账带着它跑，最多一小时一次。

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/broker"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
)

const day = 24 * time.Hour

// workspaceWithFiles 像 Workspace API 建的工作区：目录里有文件，工作区表里有一行。
func workspaceWithFiles(t *testing.T, h *harness, tenantID, ws string) string {
	t.Helper()
	root := h.mkWorkspace(tenantID, ws, map[string]string{"README.md": "hello\n", "src/app.py": "x = 1\n"})
	if err := h.svc.Broker().Archiver().Ensure(context.Background(), tenantID, ws, registry.WorkspaceLimits{MaxWorkspaces: 20, MaxBytes: 1 << 30}); err != nil {
		t.Fatal(err)
	}
	return root
}

func workspaceState(t *testing.T, h *harness, tenantID, ws string) string {
	t.Helper()
	w, err := h.svc.Broker().Archiver().Get(context.Background(), tenantID, ws)
	if err != nil || w == nil {
		t.Fatalf("工作区 %s：%v", ws, err)
	}
	return w.State
}

func cool(t *testing.T, h *harness, defaultDays int) int {
	t.Helper()
	b := h.svc.Broker()
	b.SetCold(broker.ColdOptions{DefaultDays: defaultDays, Runtime: "python", Every: time.Nanosecond})
	n, err := b.CoolIdleWorkspaces(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func TestAnIdleWorkspaceGoesColdAndComesBack(t *testing.T) {
	h := newHarness(t)
	local := workspaceWithFiles(t, h, "t1", "w1")
	h.clock.Advance(4 * day)
	if n := cool(t, h, 3); n != 1 {
		t.Fatalf("cooled = %d", n)
	}
	if _, err := os.Stat(local); !os.IsNotExist(err) {
		t.Fatal("核对过快照才删本地目录")
	}
	if workspaceState(t, h, "t1", "w1") != "cold" {
		t.Fatal("状态记成 cold")
	}
	tc := tenant("t1", "w1", "")
	warm := h.mustAcquire(tc, onWorkspace("w1"))
	if warm.GetRestoredSnapshot() == "" || h.mustExec(tc, warm, "cat src/app.py") != "x = 1\n" {
		t.Fatal("下次用到时从快照恢复")
	}
	if workspaceState(t, h, "t1", "w1") != "active" {
		t.Fatal("恢复后是 active")
	}
}

func TestRecentlyUsedOrHeldWorkspacesStay(t *testing.T) {
	h := newHarness(t)
	local := workspaceWithFiles(t, h, "t1", "w1")
	h.clock.Advance(day)
	if n := cool(t, h, 3); n != 0 {
		t.Fatal("没闲置够天数")
	}
	tc := tenant("t1", "w1", s1)
	held := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	if _, err := h.release(tc, held, true); err != nil {
		t.Fatal(err)
	}
	h.clock.Advance(10 * day)
	if n := cool(t, h, 3); n != 0 {
		t.Fatal("还有环境占着")
	}
	if _, err := os.Stat(filepath.Join(local, "README.md")); err != nil || workspaceState(t, h, "t1", "w1") != "active" {
		t.Fatal("不动")
	}
}

func TestTenantPolicyOverridesTheDeploymentDefault(t *testing.T) {
	for _, tc := range []struct {
		name              string
		policy            map[string]any
		defaultDays, idle int
		cooled            int
	}{
		{"都没配：不转冷", nil, 0, 400, 0},
		{"只有租户策略", map[string]any{"workspace": map[string]any{"cold_after_days": 2.0}}, 0, 3, 1},
		{"租户策略优先于部署的默认值", map[string]any{"workspace": map[string]any{"cold_after_days": 30.0}}, 1, 5, 0},
		{"租户没写天数：用部署的默认值", map[string]any{"workspace": map[string]any{"max_workspaces": 5.0}}, 2, 3, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var opts []option
			if tc.policy != nil {
				opts = append(opts, withPolicy("t1", tc.policy))
			}
			h := newHarness(t, opts...)
			workspaceWithFiles(t, h, "t1", "w1")
			h.clock.Advance(time.Duration(tc.idle) * day)
			if n := cool(t, h, tc.defaultDays); n != tc.cooled {
				t.Fatalf("cooled = %d，要 %d", n, tc.cooled)
			}
		})
	}
}

func TestArchiveModeTenantsAreLeftAlone(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	workspaceWithFiles(t, h, "t1", "w1")
	h.clock.Advance(30 * day)
	if n := cool(t, h, 1); n != 0 || workspaceState(t, h, "t1", "w1") != "active" {
		t.Fatal("存档模式的租户不动")
	}
}

func TestAFailedSnapshotKeepsTheLocalCopy(t *testing.T) {
	h := newHarness(t)
	local := workspaceWithFiles(t, h, "t1", "w1")
	h.clock.Advance(4 * day)
	h.faults["local"].FailNext("export_archive", providererr.Transient)
	if n := cool(t, h, 3); n != 0 {
		t.Fatal("存档失败时不算转冷")
	}
	if data, err := os.ReadFile(filepath.Join(local, "README.md")); err != nil || string(data) != "hello\n" {
		t.Fatal("存档失败时本地目录不删")
	}
	if workspaceState(t, h, "t1", "w1") != "active" {
		t.Fatal("状态回到 active")
	}
}

func TestLeasingWhileCoolingIsATransientError(t *testing.T) {
	h := newHarness(t)
	workspaceWithFiles(t, h, "t1", "w1")
	ctx := context.Background()
	if ok, err := h.svc.Broker().Archiver().Transition(ctx, "t1", "w1", "active", "cooling"); !ok || err != nil {
		t.Fatalf("%v %v", ok, err)
	}
	_, err := h.acquire(tenant("t1", "w1", ""), onWorkspace("w1"))
	if cat, c := code(t, err); cat != providererr.Transient || c != "workspace_cooling" {
		t.Fatalf("转冷进行中：%v %s", cat, c)
	}
	if ok, _ := h.svc.Broker().Archiver().Transition(ctx, "t1", "w1", "active", "cold"); ok {
		t.Fatal("状态不对时不转")
	}
}

func TestTheSweepRunsAtMostHourly(t *testing.T) {
	h := newHarness(t)
	workspaceWithFiles(t, h, "t1", "w1")
	b := h.svc.Broker()
	b.SetCold(broker.ColdOptions{DefaultDays: 3, Runtime: "python"})
	h.clock.Advance(4 * day)
	ctx := context.Background()
	if n, _ := b.CoolIdleWorkspaces(ctx); n != 1 {
		t.Fatalf("cooled = %d", n)
	}
	workspaceWithFiles(t, h, "t1", "w2")
	// w2 也闲置够了：工作区表里最后一次改动在 4 天前（目录的修改时间是真实时间，更早）。
	h.execSQL("t1", "UPDATE workspaces SET updated_at = $1 WHERE workspace_id = 'w2'", ledger.ISO(h.clock.Now().Add(-4*day)))
	h.clock.Advance(15 * time.Minute)
	if n, _ := b.CoolIdleWorkspaces(ctx); n != 0 || workspaceState(t, h, "t1", "w2") != "active" {
		t.Fatal("十几分钟之后：这一轮不扫")
	}
	h.clock.Advance(time.Hour)
	if n, _ := b.CoolIdleWorkspaces(ctx); n != 1 || workspaceState(t, h, "t1", "w2") != "cold" {
		t.Fatal("过了一小时：再扫")
	}
}
