package server_test

// 原来 Worker 里 Python Broker 的测试搬到服务这边（SBX-01：借还、配额、回退、环境表与对账都在服务里）。来源：
// py-worker 的 test_sandbox_quota.py（D-12）、test_sandbox_backend_registry.py（D-19）、test_sandbox_ledger.py（D-11）、
// test_n05_hold_while_waiting.py（N-05 第 3 条）与 test_environment_status.py（ENV-01）。Worker 一侧看得到的部分（工具的结果与
// span）留在 py-worker，对着真的服务进程跑。

import (
	"context"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/broker"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
)

// --- D-12：配额、预算与有序回退 ---

func TestDailySandboxSecondsCountRunningEnvironments(t *testing.T) {
	h := newHarness(t, withPolicy("ta", map[string]any{"sandbox": map[string]any{"backend": "local", "quota": map[string]any{"daily_sandbox_seconds": 60.0}}}))
	first := h.mustAcquire(tenant("ta", "", ""))
	if _, err := h.release(tenant("ta", "", ""), first, false); err != nil {
		t.Fatal(err)
	}
	h.execSQL("ta", "INSERT INTO sandbox_usage (day, sandbox_seconds) VALUES ($1, $2) ON CONFLICT (day) DO UPDATE SET sandbox_seconds = $2",
		h.clock.Now().UTC().Format("2006-01-02"), 61)
	_, err := h.acquire(tenant("ta", "", ""))
	if cat, c := code(t, err); cat != providererr.Denied || c != "sandbox_quota_exceeded" {
		t.Fatalf("当天的沙箱秒数用完了：%v %s", cat, c)
	}
}

func TestExplicitMinIsolationCanRaiseTheFloor(t *testing.T) {
	h := newHarness(t, withBackends(fourBackends), withPolicy("ta", map[string]any{"sandbox": map[string]any{
		"backend": "gvisor", "fallback": []any{"kata"}, "min_isolation": "microvm"}}))
	if lease := h.mustAcquire(tenant("ta", "", "")); lease.GetBackend().GetName() != "kata" {
		t.Fatalf("首选本身低于 min_isolation 时直接用回退里够强的：%v", lease.GetBackend())
	}
}

const firecrackerLike = `{"default": "gvisor", "backends": {
  "gvisor": {"kind": "local", "isolation": "container"},
  "firecracker": {"kind": "local", "isolation": "microvm"},
  "kata": {"kind": "local", "isolation": "microvm", "enabled": false}}}`

func TestStrongerBackendFallsBackOnlyWithAnExplicitMinIsolation(t *testing.T) {
	// D-19 第 4 条：首选 microvm 的后端、回退 [gvisor]、没写 min_isolation 时绝不降级；写了 min_isolation: container 才回退。
	strict := newHarness(t, withBackends(firecrackerLike), withPolicy("t1", map[string]any{"sandbox": map[string]any{
		"backend": "firecracker", "fallback": []any{"gvisor"}}}))
	strict.faults["firecracker"].FailNext("create", providererr.Transient)
	_, err := strict.acquire(tenant("t1", "", ""))
	if cat, _ := code(t, err); cat != providererr.Transient || !strings.Contains(err.Error(), "gvisor") {
		t.Fatalf("说明更弱的 gvisor 不用：%v", err)
	}
	relaxed := newHarness(t, withBackends(firecrackerLike), withPolicy("t1", map[string]any{"sandbox": map[string]any{
		"backend": "firecracker", "fallback": []any{"gvisor"}, "min_isolation": "container"}}))
	relaxed.faults["firecracker"].FailNext("create", providererr.Transient)
	lease := relaxed.mustAcquire(tenant("t1", "", ""))
	if lease.GetBackend().GetName() != "gvisor" || lease.GetFallbackFrom() != "firecracker" || !strings.HasPrefix(lease.GetFallbackReason(), "firecracker：") {
		t.Fatalf("回退并记下原因：%v", lease)
	}
}

func TestDisabledFallbackBackendIsSkipped(t *testing.T) {
	h := newHarness(t, withBackends(firecrackerLike), withPolicy("t1", map[string]any{"sandbox": map[string]any{
		"backend": "firecracker", "fallback": []any{"kata"}}}))
	h.faults["firecracker"].FailNext("create", providererr.Transient)
	_, err := h.acquire(tenant("t1", "", ""))
	if cat, _ := code(t, err); cat != providererr.Transient || !strings.Contains(err.Error(), "kata") || !strings.Contains(err.Error(), "停用") {
		t.Fatalf("停用的回退后端跳过并说明：%v", err)
	}
}

func TestDisabledBackendDrains(t *testing.T) {
	// D-19 第 3 条：后端设为 enabled: false（排空）之后新建被拒；已有的环境照常复用、执行、销毁；对账不当孤儿删、不记丢失。
	h := newHarness(t, withBackends(`{"default": "local", "backends": {"cf": {"kind": "local", "isolation": "microvm"}, "local": {"kind": "local", "isolation": "none"}}}`),
		withPolicy("t1", map[string]any{"sandbox": map[string]any{"backend": "cf"}}))
	tc := tenant("t1", "", s1)
	lease := h.mustAcquire(tc, reuse())
	if _, err := h.release(tc, lease, true); err != nil {
		t.Fatal(err)
	}
	// 滚动重启后的服务：登记里 cf 停用了。
	h.backends = `{"default": "local", "backends": {"cf": {"kind": "local", "isolation": "microvm", "enabled": false}, "local": {"kind": "local", "isolation": "none"}}}`
	h.restart()
	_, err := h.acquire(tenant("t1", "", s2), reuse())
	if cat, c := code(t, err); cat != providererr.Denied || c != "sandbox_backend_disabled" || !strings.Contains(err.Error(), "cf") || !strings.Contains(err.Error(), "登记") {
		t.Fatalf("新建被拒并说明是登记停用的：%v", err)
	}
	again := h.mustAcquire(tc, reuse())
	if !again.GetReused() || again.GetLeaseId() != lease.GetLeaseId() {
		t.Fatalf("已有的环境照常复用：%v", again)
	}
	if out := h.mustExec(tc, again, "printf 'still here' > drain.txt && cat drain.txt"); out != "still here" {
		t.Fatalf("已有的环境照常可用：%q", out)
	}
	if _, err := h.release(tc, again, true); err != nil {
		t.Fatal(err)
	}
	report, err := h.svc.Reconcile(context.Background())
	if err != nil || report.OrphansDestroyed != 0 || report.Lost != 0 || report.Reaped != 0 {
		t.Fatalf("对账不删排空中的环境：%+v %v", report, err)
	}
	h.clock.Advance(time.Hour)
	if report, _ = h.svc.Reconcile(context.Background()); report.Reaped != 1 {
		t.Fatalf("空闲到期照常回收：%+v", report)
	}
	if h.envState("t1", lease.GetLeaseId()) != "destroyed" {
		t.Fatal("销毁正常")
	}
}

// --- D-11：环境表与对账 ---

func TestLifecycleIsRecordedWithCostAndUsage(t *testing.T) {
	h := newHarness(t, withBackends(`{"default": "local", "backends": {"local": {"kind": "local", "isolation": "none", "price": {"per_hour": 3.6}}}}`))
	h.mkWorkspace("ta", "w1", nil)
	tc := tenant("ta", "w1", "")
	lease := h.mustAcquire(tc, onWorkspace("w1"), taskID("task-1"))
	h.clock.Advance(30 * time.Minute)
	if _, err := h.release(tc, lease, false); err != nil {
		t.Fatal(err)
	}
	row := h.rows("ta", "SELECT state, task_id, workspace_id, alive_ms, cost_usd FROM sandbox_envs WHERE env_ref = $1", lease.GetLeaseId())[0]
	alive, _ := strconv.ParseInt(fmt.Sprint(row["alive_ms"]), 10, 64)
	cost, _ := strconv.ParseFloat(fmt.Sprint(row["cost_usd"]), 64)
	if row["state"] != "destroyed" || row["task_id"] != "task-1" || row["workspace_id"] != "w1" {
		t.Fatalf("row = %v", row)
	}
	if alive < 30*60_000 || alive >= 31*60_000 {
		t.Fatalf("alive_ms = %d", alive)
	}
	if want := float64(alive) / 3_600_000 * 3.6; math.Abs(cost-want) > want*1e-3 {
		t.Fatalf("按存活时间与价目算花费：%v，要 %v", cost, want)
	}
	usage := h.rows("ta", "SELECT sandbox_seconds, cost_usd FROM sandbox_usage WHERE day = $1", h.clock.Now().UTC().Format("2006-01-02"))
	if len(usage) != 1 || fmt.Sprint(usage[0]["sandbox_seconds"]) != strconv.FormatInt(alive/1000, 10) {
		t.Fatalf("当天的用量：%v", usage)
	}
}

func TestCrashAfterBackendCreateIsRepairedWithoutOrphans(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	// 服务在后端建好环境、还没记 ready 时被杀：环境表里是 creating，后端里有一个它不认识的环境。
	rec, err := h.svc.Broker().Ledger().BeginCreate(ctx, "ta", ledger.CreateArgs{Backend: "local", Kind: "local", Isolation: "none", Runtime: "python",
		Idle: 10 * time.Minute, MaxLifetime: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := h.box("local", "ta").Create(ctx, sandbox.Spec{}); err != nil {
		t.Fatal(err)
	}
	report, err := h.svc.Reconcile(ctx)
	if err != nil || report.OrphansDestroyed != 0 || report.StaleFailed != 0 {
		t.Fatalf("宽限之内什么都不动：%+v %v", report, err)
	}
	h.clock.Advance(11 * time.Minute)
	if report, _ = h.svc.Reconcile(ctx); report.StaleFailed != 1 || report.OrphansDestroyed != 1 {
		t.Fatalf("过了宽限：creating 记为失败、孤儿删掉：%+v", report)
	}
	if h.envState("ta", rec.EnvRef) != "failed" {
		t.Fatal("creating 的那一行记为失败")
	}
	if dir := h.rows("_platform", "SELECT env_ref FROM sandbox_env_directory WHERE state NOT IN ('destroyed', 'failed', 'lost')"); len(dir) != 0 {
		t.Fatalf("目录里不再有没结束的环境：%v", dir)
	}
}

func TestCrashBetweenDestroyingAndBackendDeleteIsRetried(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	lease := h.mustAcquire(tenant("ta", "", ""))
	rec, err := h.svc.Broker().Ledger().Get(ctx, "ta", lease.GetLeaseId())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := h.svc.Broker().Ledger().BeginDestroy(ctx, *rec, "服务删到一半退出"); err != nil {
		t.Fatal(err)
	}
	report, err := h.svc.Reconcile(ctx)
	if err != nil || report.DestroyRetried != 1 {
		t.Fatalf("重做删除：%+v %v", report, err)
	}
	if h.envState("ta", lease.GetLeaseId()) != "destroyed" {
		t.Fatal("删完记 destroyed")
	}
}

func TestFreshUnrecordedEnvironmentIsLeftAloneUntilTheGracePasses(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	if _, err := h.box("local", "ta").Create(ctx, sandbox.Spec{}); err != nil { // 绕过服务：环境表里没有它
		t.Fatal(err)
	}
	if report, _ := h.svc.Reconcile(ctx); report.OrphansDestroyed != 0 {
		t.Fatalf("刚建的、还没记进环境表的环境不删：%+v", report)
	}
	h.clock.Advance(10*time.Minute + time.Second)
	if report, _ := h.svc.Reconcile(ctx); report.OrphansDestroyed != 1 {
		t.Fatalf("过了宽限才当孤儿删：%+v", report)
	}
}

func TestIdleSessionEnvironmentIsReapedButABusyOneIsNot(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("ta", "w1", nil)
	h.mkWorkspace("ta", "w2", nil)
	idleTC, busyTC := tenant("ta", "w1", s1), tenant("ta", "w2", s1)
	idle := h.mustAcquire(idleTC, onWorkspace("w1"), reuse())
	if _, err := h.release(idleTC, idle, true); err != nil {
		t.Fatal(err)
	}
	busy := h.mustAcquire(busyTC, onWorkspace("w2"), reuse(), busyFor(time.Hour))
	h.clock.Advance(16 * time.Minute)
	if report, _ := h.svc.Reconcile(context.Background()); report.Reaped != 1 {
		t.Fatalf("只回收闲着的：%+v", report)
	}
	if h.envState("ta", idle.GetLeaseId()) != "destroyed" || h.envState("ta", busy.GetLeaseId()) != "ready" {
		t.Fatal("命令还在跑的环境不回收")
	}
}

func TestTenantDeletionDestroysEveryEnvironment(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	var leases []string
	for range 3 {
		leases = append(leases, h.mustAcquire(tenant("ta", "", "")).GetLeaseId())
	}
	keep := h.mustAcquire(tenant("tb", "", ""))
	n, err := h.svc.Broker().DestroyTenant(ctx, "ta")
	if err != nil || n != 3 {
		t.Fatalf("删租户销毁它的全部环境：%d %v", n, err)
	}
	for _, ref := range leases {
		if h.envState("ta", ref) != "destroyed" {
			t.Fatalf("%s 没销毁", ref)
		}
	}
	if h.envState("tb", keep.GetLeaseId()) != "ready" {
		t.Fatal("别的租户的不动")
	}
}

// --- N-05 第 3 条：等人时环境不丢 ---

const tasksDDL = "CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, outcome TEXT NOT NULL DEFAULT '')"

func sessionEnvOfTask(t *testing.T, h *harness, task string) string {
	t.Helper()
	h.mkWorkspace("ta", "w1", nil)
	tc := tenant("ta", "w1", s1)
	lease := h.mustAcquire(tc, onWorkspace("w1"), reuse(), taskID(task))
	if _, err := h.release(tc, lease, true); err != nil {
		t.Fatal(err)
	}
	return lease.GetLeaseId()
}

func TestAnEnvironmentIsKeptWhileItsTaskWaits(t *testing.T) {
	h := newHarness(t)
	ref := sessionEnvOfTask(t, h, "tsk_waiting")
	h.execSQL("ta", tasksDDL)
	h.execSQL("ta", "INSERT INTO tasks (task_id, outcome) VALUES ('tsk_waiting', '')")
	h.clock.Advance(35 * time.Minute)
	report, err := h.svc.Reconcile(context.Background())
	if err != nil || report.Reaped != 0 || report.Held != 1 {
		t.Fatalf("Task 还在等人：环境留着：%+v %v", report, err)
	}
	if h.envState("ta", ref) != "ready" {
		t.Fatal("回来接着用")
	}
}

func TestTheHoldHasACeiling(t *testing.T) {
	h := newHarness(t)
	ref := sessionEnvOfTask(t, h, "tsk_waiting")
	h.execSQL("ta", tasksDDL)
	h.execSQL("ta", "INSERT INTO tasks (task_id, outcome) VALUES ('tsk_waiting', '')")
	for range 9 {
		h.clock.Advance(16 * time.Minute)
		_, _ = h.svc.Reconcile(context.Background())
	}
	if h.envState("ta", ref) != "destroyed" {
		t.Fatal("从最后一次使用算起超过 2 小时（AR_SANDBOX_HOLD_MAX_S 的默认值）就回收")
	}
}

func TestAFinishedTaskDoesNotHoldItsEnvironment(t *testing.T) {
	h := newHarness(t)
	ref := sessionEnvOfTask(t, h, "tsk_done")
	h.execSQL("ta", tasksDDL)
	h.execSQL("ta", `INSERT INTO tasks (task_id, outcome) VALUES ('tsk_done', '{"status":"TASK_STATUS_DONE"}')`)
	h.clock.Advance(16 * time.Minute)
	if report, _ := h.svc.Reconcile(context.Background()); report.Reaped != 1 || report.Held != 0 {
		t.Fatalf("结束了的 Task 不留环境：%+v", report)
	}
	if h.envState("ta", ref) != "destroyed" {
		t.Fatal("回收了")
	}
}

func TestWithoutTheTaskTableEnvironmentsAreReapedAsBefore(t *testing.T) {
	// 服务与 task-service 不用同一个库（表不在）：按原来的规则回收，对账不报错。
	h := newHarness(t)
	sessionEnvOfTask(t, h, "tsk_unknown")
	h.clock.Advance(16 * time.Minute)
	if report, err := h.svc.Reconcile(context.Background()); err != nil || report.Reaped != 1 || len(report.Errors) != 0 {
		t.Fatalf("%+v %v", report, err)
	}
}

// --- ENV-01：健康确认与复借是环境表里明确的事实 ---

type confirming struct {
	sandbox.Provider
	mu     *sync.Mutex
	health *[]string
}

func (p confirming) Bind(tc *commonv1.TenantContext) (sandbox.Sandbox, error) {
	inner, err := p.Provider.Bind(tc)
	return confirmingBox{Sandbox: inner, p: p}, err
}

type confirmingBox struct {
	sandbox.Sandbox
	p confirming
}

func (b confirmingBox) Create(ctx context.Context, spec sandbox.Spec) (sandbox.Env, error) {
	env, err := b.Sandbox.Create(ctx, spec)
	env.HealthConfirmed = err == nil
	return env, err
}

func (b confirmingBox) Healthy(_ context.Context, envID string) bool {
	b.p.mu.Lock()
	defer b.p.mu.Unlock()
	*b.p.health = append(*b.p.health, envID)
	return true
}

func TestHealthAndRealSessionReuseAreRecorded(t *testing.T) {
	var mu sync.Mutex
	var health []string
	h := newHarness(t, withWrappedProvider("local", func(p sandbox.Provider) sandbox.Provider { return confirming{Provider: p, mu: &mu, health: &health} }))
	h.mkWorkspace("t1", "w1", nil)
	tc := tenant("t1", "w1", s1)
	first := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	row := h.rows("t1", "SELECT health_confirmed_at, reused_at FROM sandbox_envs WHERE env_ref = $1", first.GetLeaseId())[0]
	if fmt.Sprint(row["health_confirmed_at"]) == "" || fmt.Sprint(row["reused_at"]) != "" {
		t.Fatalf("建环境时做过健康检查才记确认；还没复借：%v", row)
	}
	if _, err := h.release(tc, first, true); err != nil {
		t.Fatal(err)
	}
	h.clock.Advance(time.Second)
	h.restart() // 另一个副本：读同一张环境表，不靠进程内的标记
	second := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	if second.GetLeaseId() != first.GetLeaseId() {
		t.Fatal("同会话复借同一个环境")
	}
	row = h.rows("t1", "SELECT health_confirmed_at, reused_at FROM sandbox_envs WHERE env_ref = $1", first.GetLeaseId())[0]
	now := ledger.ISO(h.clock.Now())
	if fmt.Sprint(row["reused_at"]) != now || fmt.Sprint(row["health_confirmed_at"]) != now {
		t.Fatalf("复借时探测健康并记下：%v，要 %s", row, now)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(health) != 1 {
		t.Fatalf("只在真正复借时探测：%v", health)
	}
}

// --- D-15：包镜像的地址与环境凭据（原 Worker 的 test_sandbox_mirror.py） ---

func envOf(t *testing.T, h *harness, tc *commonv1.TenantContext, lease interface{ GetLeaseId() string }) map[string]string {
	t.Helper()
	out := h.mustExec(tc, &sandboxv1.Lease{LeaseId: lease.GetLeaseId()}, "env")
	env := map[string]string{}
	for _, line := range strings.Split(out, "\n") {
		if k, v, ok := strings.Cut(line, "="); ok {
			env[k] = v
		}
	}
	return env
}

func TestCommandsGetTheMirrorAndACredentialForTheirEnvironment(t *testing.T) {
	h := newHarness(t, withGateways("http://pkg-mirror.test:8093", ""),
		withPolicy("t1", map[string]any{"sandbox": map[string]any{"backend": "local", "quota": map[string]any{"daily_mirror_miss_bytes": 1073741824.0}}}))
	h.mkWorkspace("t1", "w1", nil)
	tc := tenant("t1", "w1", "")
	lease := h.mustAcquire(tc, onWorkspace("w1"))
	env := envOf(t, h, tc, lease)
	index := env["PIP_INDEX_URL"]
	token, ok := strings.CutPrefix(index, "http://pkg-mirror.test:8093/t/")
	token, ok2 := strings.CutSuffix(token, "/pypi/simple/")
	if !ok || !ok2 || !strings.HasPrefix(token, "ar1.") {
		t.Fatalf("PIP_INDEX_URL = %q", index)
	}
	if env["npm_config_registry"] != "http://pkg-mirror.test:8093/t/"+token+"/npm/" || env["GOPROXY"] != "http://pkg-mirror.test:8093/t/"+token+"/go" {
		t.Fatalf("npm 与 Go 的镜像：%v", env)
	}
	if env["PIP_TRUSTED_HOST"] != "pkg-mirror.test" || env["npm_config_audit"] != "false" {
		t.Fatalf("http 的镜像要 trusted-host、npm 不审计：%v", env)
	}
	claims, err := envcred.Verify([]byte("ZW52LWNyZWRlbnRpYWwta2V5LTAxMjM0NTY3ODlhYmNkZWY="), token, envcred.PurposeMirror, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if claims.TenantID != "t1" || claims.EnvRef != lease.GetLeaseId() || claims.WorkspaceID != "w1" || claims.Limits["mirror_miss_bytes"] != 1073741824 {
		t.Fatalf("凭据绑定这个环境，限额取自租户策略：%+v", claims)
	}
	if rows := fmt.Sprint(h.rows("t1", "SELECT * FROM sandbox_envs")); strings.Contains(rows, token) {
		t.Fatal("凭据不进环境表")
	}
}

func TestRegisteredMirrorWinsAndHostedBackendsGetNoneByDefault(t *testing.T) {
	h := newHarness(t, withGateways("http://pkg-mirror.test:8093", ""),
		withBackends(`{"default": "remote", "backends": {"remote": {"kind": "local", "isolation": "none", "pkg_mirror_url": "https://mirror.example.com"}}}`))
	tc := tenant("t1", "", "")
	lease := h.mustAcquire(tc)
	env := envOf(t, h, tc, lease)
	if !strings.HasPrefix(env["PIP_INDEX_URL"], "https://mirror.example.com/t/ar1.") || env["PIP_TRUSTED_HOST"] != "" {
		t.Fatalf("登记里的 pkg_mirror_url 优先；https 时不加 trusted-host：%v", env)
	}
	creds := broker.NewCredentials(h.svc.Registry(), "http://pkg-mirror:8093", "")
	if creds.MirrorFor(registry.Spec{"kind": "cloudflare"}) != "" || creds.MirrorFor(registry.Spec{"kind": "k8s"}) != "http://pkg-mirror:8093" {
		t.Fatal("托管后端默认不注入（断网时连不到集群里的镜像），K8s 与本地注入")
	}
}

// --- D-14：环境表记运行环境与镜像（原 Worker 的 test_sandbox_runtime.py） ---

const digestV1 = "ghcr.io/agentruntime/sandbox-python@sha256:1111111111111111111111111111111111111111111111111111111111111111"
const digestV2 = "ghcr.io/agentruntime/sandbox-python@sha256:2222222222222222222222222222222222222222222222222222222222222222"

func withImage(image string) string {
	return `{"default": "local", "backends": {"local": {"kind": "local", "isolation": "none", "image": "` + image + `"}}}`
}

func TestConfiguredImageIsRecordedAndOldRowsKeepTheirs(t *testing.T) {
	h := newHarness(t, withBackends(withImage(digestV1)))
	tc := tenant("t1", "", "")
	first := h.mustAcquire(tc)
	if first.GetRecord().GetImage() != digestV1 || first.GetRecord().GetRuntime() != "python" {
		t.Fatalf("记登记里配的镜像：%v", first.GetRecord())
	}
	if _, err := h.release(tc, first, false); err != nil {
		t.Fatal(err)
	}
	h.backends = withImage(digestV2) // 换了镜像版本（同一个业务库）
	h.restart()
	if second := h.mustAcquire(tc); second.GetRecord().GetImage() != digestV2 {
		t.Fatalf("新的用新镜像：%v", second.GetRecord())
	}
	if rows := h.rows("t1", "SELECT image FROM sandbox_envs WHERE env_ref = $1", first.GetLeaseId()); fmt.Sprint(rows[0]["image"]) != digestV1 {
		t.Fatal("以前的行不变")
	}
}

type reportsImage struct{ sandbox.Provider }

func (p reportsImage) Bind(tc *commonv1.TenantContext) (sandbox.Sandbox, error) {
	inner, err := p.Provider.Bind(tc)
	return reportsImageBox{inner}, err
}

type reportsImageBox struct{ sandbox.Sandbox }

func (b reportsImageBox) Create(ctx context.Context, spec sandbox.Spec) (sandbox.Env, error) {
	env, err := b.Sandbox.Create(ctx, spec)
	env.Image = digestV1
	return env, err
}

func TestTheImageReportedByTheBackendWins(t *testing.T) {
	h := newHarness(t, withBackends(withImage("ghcr.io/agentruntime/sandbox-python:0.1.0")),
		withWrappedProvider("local", func(p sandbox.Provider) sandbox.Provider { return reportsImage{p} }))
	if lease := h.mustAcquire(tenant("t1", "", "")); lease.GetRecord().GetImage() != digestV1 {
		t.Fatalf("标签换成后端报的 digest：%v", lease.GetRecord())
	}
}

// --- 挂卷：借环境之前由服务建好工作区目录（原 Worker 的 test_workspace_operations.py） ---

type dirWatcher struct {
	sandbox.Provider
	root func() string
	seen *[]bool
}

func (p dirWatcher) Bind(tc *commonv1.TenantContext) (sandbox.Sandbox, error) {
	inner, err := p.Provider.Bind(tc)
	return dirWatcherBox{Sandbox: inner, p: p, owner: tc.GetTenantId()}, err
}

type dirWatcherBox struct {
	sandbox.Sandbox
	p     dirWatcher
	owner string
}

func (b dirWatcherBox) Create(ctx context.Context, spec sandbox.Spec) (sandbox.Env, error) {
	if spec.WorkspaceID != "" {
		st, err := os.Stat(filepath.Join(b.p.root(), b.owner, spec.WorkspaceID))
		*b.p.seen = append(*b.p.seen, err == nil && st.IsDir())
	}
	return b.Sandbox.Create(ctx, spec)
}

func TestMountModeCreatesTheWorkspaceDirBeforeBorrowingAnEnv(t *testing.T) {
	// K8s 后端按 subPath 挂工作区：目录要由服务先建好，否则 kubelet 以 root 建，沙箱写不进去。
	var seen []bool
	var h *harness
	h = newHarness(t, withWrappedProvider("local", func(p sandbox.Provider) sandbox.Provider {
		return dirWatcher{Provider: p, root: func() string { return h.workspaces }, seen: &seen}
	}))
	h.mustAcquire(tenant("t1", "w-new", ""), onWorkspace("w-new"))
	if len(seen) != 1 || !seen[0] {
		t.Fatalf("建环境时工作区目录已经在了：%v", seen)
	}
}
