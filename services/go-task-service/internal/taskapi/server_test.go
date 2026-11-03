package taskapi

// S-03 验收：Task API（设计书 3.1.1、2.5；计划 S-03 交付“Task API 的 gRPC 实现：提交、取消、信号、查询”）。
//
// 覆盖：认证与租户（CLAUDE.md 租户隔离：租户由服务端根据认证身份确定，请求里的 tenant_id 只用来校验一致；
// 访问别的租户的数据按不存在处理），投递到 Session 工作流，重复提交返回同一个 task_id（计划 S-03 难点“子工作流 ID”），
// 取消与人工回复转为信号，查询读取工作流内的 Task State，错误码映射，HTTP + JSON 可用（设计书 4.15）。
//
// DurableEngine 用 fakeDurable 记录调用；Ingress、RelationalStore 用开发实现。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1/taskapiv1connect"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/ids"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

const baseURL = "https://api.example.test"

var (
	signingKey     = []byte("test-signing-key-0123456789abcdef")
	sessionPattern = regexp.MustCompile(`^ses_[0-7][0-9A-HJKMNP-TV-Z]{25}$`)
	taskPattern    = regexp.MustCompile(`^tsk_[0-7][0-9A-HJKMNP-TV-Z]{25}$`)
)

type staticPins struct {
	mu      sync.Mutex
	v       *workflowv1.PinnedVersions
	tenants []string
}

func (p *staticPins) Resolve(_ context.Context, tenant *commonv1.TenantContext) (*workflowv1.PinnedVersions, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.tenants = append(p.tenants, tenant.GetTenantId())
	return proto.Clone(p.v).(*workflowv1.PinnedVersions), nil
}

type apiFixture struct {
	t       *testing.T
	durable *fakeDurable
	ingress *localingress.Provider
	store   *sqlitestore.Provider
	tasks   *taskstore.Store
	clock   *testkit.FakeClock
	pins    *staticPins
	client  taskapiv1connect.TaskServiceClient
	url     string
}

func newAPI(t *testing.T, ingressOpts ...func(*localingress.Options)) *apiFixture {
	t.Helper()
	return newAPIWith(t, nil, ingressOpts...)
}

// newAPIWith 同 newAPI，另外可以改 Server 的依赖（例如换上租户策略）。
func newAPIWith(t *testing.T, deps func(*Deps), ingressOpts ...func(*localingress.Options)) *apiFixture {
	t.Helper()
	clk := testkit.NewFakeClock()
	io := localingress.Options{SigningKey: signingKey, Clock: clk}
	for _, o := range ingressOpts {
		o(&io)
	}
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	if err := store.Migrate(context.Background(), taskstore.Migrations()); err != nil {
		t.Fatal(err)
	}
	f := &apiFixture{
		t: t, durable: newFakeDurable(), ingress: localingress.New(io), store: store,
		tasks: taskstore.New(store), clock: clk,
		pins: &staticPins{v: &workflowv1.PinnedVersions{
			Recipe:        "stub@v1",
			NodeTemplates: map[string]string{"stub_answer": "v2"},
			ModelProfiles: map[string]string{"mock/model": "v1"},
			TenantPolicy:  "v5",
		}},
	}
	d := Deps{
		Ingress: f.ingress, Durable: f.durable, Store: store, Pins: f.pins, Clock: clk,
		IDs: ids.New(clk, nil), PublicBaseURL: baseURL,
		Defaults: Defaults{Budget: &workflowv1.Budget{UsdMicros: 2_000_000, ToolCalls: 50}, EffectCeiling: commonv1.Effect_EFFECT_READ},
	}
	if deps != nil {
		deps(&d)
	}
	srv, err := New(d)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	srv.Mount(mux)
	hs := httptest.NewServer(mux)
	t.Cleanup(hs.Close)
	f.client = taskapiv1connect.NewTaskServiceClient(hs.Client(), hs.URL)
	f.url = hs.URL
	return f
}

func (f *apiFixture) token(tenant, user string) string {
	return f.ingress.IssueToken(tenant, user, time.Hour)
}

func authed[T any](token string, msg *T) *connect.Request[T] {
	req := connect.NewRequest(msg)
	if token != "" {
		req.Header().Set("Authorization", "Bearer "+token)
	}
	return req
}

func (f *apiFixture) submit(token string, req *taskapiv1.SubmitTaskRequest) (*taskapiv1.SubmitTaskResponse, error) {
	resp, err := f.client.SubmitTask(context.Background(), authed(token, req))
	if err != nil {
		return nil, err
	}
	return resp.Msg, nil
}

func (f *apiFixture) mustSubmit(token string, req *taskapiv1.SubmitTaskRequest) *taskapiv1.SubmitTaskResponse {
	f.t.Helper()
	resp, err := f.submit(token, req)
	if err != nil {
		f.t.Fatalf("提交失败：%v", err)
	}
	return resp
}

func wantCode(t *testing.T, what string, err error, code connect.Code) {
	t.Helper()
	if err == nil {
		t.Fatalf("%s：应返回 %v，实际成功", what, code)
	}
	if got := connect.CodeOf(err); got != code {
		t.Fatalf("%s：应返回 %v，实际 %v（%v）", what, code, got, err)
	}
}

func newRequest(crid string) *taskapiv1.SubmitTaskRequest {
	return &taskapiv1.SubmitTaskRequest{ClientRequestId: crid, Message: "上季度退款率为什么上升？"}
}

func seedActiveTask(t *testing.T, f *apiFixture, tenantID, taskID string) {
	t.Helper()
	_, _, err := f.tasks.InsertTask(context.Background(), &commonv1.TenantContext{TenantId: tenantID}, taskstore.Task{
		TaskID: taskID, SessionID: "ses_" + taskID, ClientRequestID: "seed_" + taskID, CreatedAt: f.clock.Now(),
	})
	if err != nil {
		t.Fatalf("seed active task %s: %v", taskID, err)
	}
}

func finishSeededTask(t *testing.T, f *apiFixture, tenantID, taskID string) {
	t.Helper()
	if err := f.tasks.RecordFinished(context.Background(), &commonv1.TenantContext{TenantId: tenantID}, taskID,
		commonv1.TaskStatus_TASK_STATUS_DONE, 1, "{}"); err != nil {
		t.Fatalf("finish seeded task %s: %v", taskID, err)
	}
}

// 准入时未终态 Task 的上限（docs/plan/open-signup.md）：2026-11-03 人定默认不设上限（写死 2 个太死，不同类型的用户该有不同策略）；
// 要限的租户由部署配置给（Deps.ActiveTaskLimits，AR_TENANT_ACTIVE_TASKS）。
func TestSubmitHasNoActiveTaskCapByDefault(t *testing.T) {
	f := newAPI(t)
	for i := 1; i <= 5; i++ {
		seedActiveTask(t, f, "t1", fmt.Sprintf("active_%d", i))
	}
	f.mustSubmit(f.token("t1", "u1"), newRequest("c6"))
}

// 给了上限的租户：检查在创建 Task 的同一事务/原子路径内做，不能用先 count 再 insert 的竞态方案。
func limitT1(d *Deps) { d.ActiveTaskLimits = map[string]int{"t1": 2} }

func TestSubmitRejectsThirdActiveTaskForTenant(t *testing.T) {
	f := newAPIWith(t, limitT1)
	seedActiveTask(t, f, "t1", "active_1")
	seedActiveTask(t, f, "t1", "active_2")
	_, err := f.submit(f.token("t1", "u1"), newRequest("c3"))
	wantCode(t, "上限 2 个的租户的第三个未终态 Task", err, connect.CodePermissionDenied)
}

func TestSubmitTenantBusyIgnoresFinishedTasks(t *testing.T) {
	f := newAPIWith(t, limitT1)
	seedActiveTask(t, f, "t1", "active_1")
	seedActiveTask(t, f, "t1", "active_2")
	seedActiveTask(t, f, "t1", "finished")
	finishSeededTask(t, f, "t1", "finished")
	_, err := f.submit(f.token("t1", "u1"), newRequest("c4"))
	wantCode(t, "两个未终态 Task 仍然达到上限", err, connect.CodePermissionDenied)

	finishSeededTask(t, f, "t1", "active_1")
	f.mustSubmit(f.token("t1", "u1"), newRequest("c5"))
}

func TestSubmitTenantBusyIsolatedBetweenTenants(t *testing.T) {
	f := newAPIWith(t, func(d *Deps) { d.ActiveTaskLimits = map[string]int{"t1": 2, "t2": 2} })
	seedActiveTask(t, f, "t1", "active_1")
	seedActiveTask(t, f, "t1", "active_2")
	f.mustSubmit(f.token("t2", "u2"), newRequest("other_tenant"))
}

func TestSubmitActiveTaskLimitIsPerTenant(t *testing.T) {
	f := newAPIWith(t, func(d *Deps) { d.ActiveTaskLimits = map[string]int{"evals": 3, "swebench": 0} })
	seedActiveTask(t, f, "evals", "e1")
	seedActiveTask(t, f, "evals", "e2")
	f.mustSubmit(f.token("evals", "u1"), newRequest("e3"))
	_, err := f.submit(f.token("evals", "u1"), newRequest("e4"))
	wantCode(t, "evals 的第四个未终态 Task", err, connect.CodePermissionDenied)

	for i := 1; i <= 5; i++ {
		seedActiveTask(t, f, "swebench", fmt.Sprintf("s%d", i))
		seedActiveTask(t, f, "t3", fmt.Sprintf("u%d", i))
	}
	f.mustSubmit(f.token("swebench", "u1"), newRequest("s6"))
	f.mustSubmit(f.token("t3", "u1"), newRequest("没列出的租户不设上限"))
}

// lastSubmitSignal 返回最后一次投递给 Session 工作流的提交信号。
func (f *apiFixture) lastSubmit() (durableCall, *workflowv1.SubmitTaskSignal) {
	f.t.Helper()
	calls := f.durable.callsOf("SignalWithStart")
	if len(calls) == 0 {
		f.t.Fatal("没有投递给 Session 工作流")
	}
	c := calls[len(calls)-1]
	sig, ok := c.Payload.(*workflowv1.SubmitTaskSignal)
	if !ok {
		f.t.Fatalf("信号载荷应为 SubmitTaskSignal，实际 %T", c.Payload)
	}
	return c, sig
}

// --- 认证与租户 ---

func TestSubmitRequiresAuthentication(t *testing.T) {
	f := newAPI(t)
	_, err := f.submit("", newRequest("c1"))
	wantCode(t, "没有凭据", err, connect.CodeUnauthenticated)
	forged := "eyJ0IjoidDEifQ.forged-signature"
	_, err = f.submit(forged, newRequest("c1"))
	wantCode(t, "签名不对的凭据", err, connect.CodeUnauthenticated)
	if strings.Contains(err.Error(), forged) {
		t.Error("错误信息不能带出凭据")
	}
	expired := f.ingress.IssueToken("t1", "u1", time.Minute)
	f.clock.Advance(2 * time.Minute)
	_, err = f.submit(expired, newRequest("c1"))
	wantCode(t, "过期的凭据", err, connect.CodeUnauthenticated)
	if len(f.durable.callsOf("SignalWithStart")) != 0 {
		t.Error("认证失败的请求不能触达 DurableEngine")
	}
}

func TestSubmitRejectsTenantMismatch(t *testing.T) {
	f := newAPI(t)
	req := newRequest("c1")
	req.TenantId = "t2"
	_, err := f.submit(f.token("t1", "u1"), req)
	wantCode(t, "请求的 tenant_id 与认证身份不一致", err, connect.CodePermissionDenied)
	if len(f.durable.callsOf("SignalWithStart")) != 0 {
		t.Error("租户不一致的请求不能触达 DurableEngine")
	}
	req.TenantId = "t1"
	f.mustSubmit(f.token("t1", "u1"), req) // 一致时可以填
}

func TestSubmitBuildsTenantContextFromIdentity(t *testing.T) {
	f := newAPI(t)
	req := newRequest("c1")
	req.WorkspaceId = "ws1"
	resp := f.mustSubmit(f.token("t1", "u1"), req)

	call, sig := f.lastSubmit()
	if call.Scope != "t1" {
		t.Errorf("DurableEngine 必须按认证身份的租户绑定，实际 %q", call.Scope)
	}
	want := &commonv1.TenantContext{TenantId: "t1", UserId: "u1", WorkspaceId: "ws1", SessionId: resp.GetSessionId()}
	if got := sig.GetTask().GetTenant(); !proto.Equal(got, want) {
		t.Errorf("Task 的租户上下文应由认证身份构造\n期望 %v\n实际 %v", want, got)
	}
	in, ok := call.Opts.Input.(*workflowv1.SessionWorkflowInput)
	if !ok || in.GetTenant().GetTenantId() != "t1" || in.GetTenant().GetSessionId() != resp.GetSessionId() {
		t.Errorf("Session 工作流的输入应带上同一租户与 session_id，实际 %v", call.Opts.Input)
	}
	if f.pins.tenants[len(f.pins.tenants)-1] != "t1" {
		t.Error("固定版本要按认证身份的租户解析")
	}
}

// --- 提交 ---

func TestSubmitNewSessionDeliversToSessionWorkflow(t *testing.T) {
	f := newAPI(t)
	resp := f.mustSubmit(f.token("t1", "u1"), newRequest("c1"))
	if !sessionPattern.MatchString(resp.GetSessionId()) || !taskPattern.MatchString(resp.GetTaskId()) {
		t.Fatalf("返回的 ID 格式不对：%v", resp)
	}
	if want := baseURL + "/v1/tasks/" + resp.GetTaskId() + "/events"; resp.GetEventsUrl() != want {
		t.Errorf("SSE 订阅地址应为 %q，实际 %q", want, resp.GetEventsUrl())
	}
	if resp.GetDeduplicated() {
		t.Error("第一次提交不是重复提交")
	}
	call, sig := f.lastSubmit()
	if call.WorkflowType != session.WorkflowName || call.WorkflowID != resp.GetSessionId() ||
		call.Opts.TaskQueue != session.SessionTaskQueue || call.Name != session.SignalSubmitTask {
		t.Errorf("应以 session_id 为 ID 对 Session 工作流 SignalWithStart（%s 队列、%s 信号），实际 %+v",
			session.SessionTaskQueue, session.SignalSubmitTask, call)
	}
	if sig.GetClientRequestId() != "c1" || sig.GetTask().GetTaskId() != resp.GetTaskId() {
		t.Errorf("提交信号应带 client_request_id 与 task_id，实际 %v", sig)
	}
}

func TestSubmitFillsTaskWorkflowInput(t *testing.T) {
	f := newAPI(t)
	inputs, _ := structpb.NewStruct(map[string]any{"quarter": "2026Q3"})
	req := newRequest("c1")
	req.Inputs = inputs
	req.Attachments = []string{"obj://t1/uploads/refunds.csv"}
	req.SessionAccess = commonv1.SessionAccess_SESSION_ACCESS_READ_ONLY
	req.Priority = commonv1.Priority_PRIORITY_BATCH
	f.mustSubmit(f.token("t1", "u1"), req)
	_, sig := f.lastSubmit()
	task := sig.GetTask()

	if !proto.Equal(task.GetPinnedVersions(), f.pins.v) {
		t.Errorf("固定的版本应写入工作流输入，实际 %v", task.GetPinnedVersions())
	}
	if !task.GetSubmittedAt().AsTime().Equal(f.clock.Now()) {
		t.Errorf("submitted_at 应取服务端时钟，实际 %v", task.GetSubmittedAt())
	}
	if task.GetMessage() != req.GetMessage() || !proto.Equal(task.GetInputs(), inputs) ||
		strings.Join(task.GetAttachments(), ",") != "obj://t1/uploads/refunds.csv" {
		t.Errorf("命令、输入与附件应原样带入，实际 %v", task)
	}
	if task.GetSessionAccess() != commonv1.SessionAccess_SESSION_ACCESS_READ_ONLY || task.GetPriority() != commonv1.Priority_PRIORITY_BATCH {
		t.Errorf("session_access 与 priority 应原样带入，实际 %v / %v", task.GetSessionAccess(), task.GetPriority())
	}
	if task.GetBudget().GetUsdMicros() != 2_000_000 || task.GetEffectCeiling() != commonv1.Effect_EFFECT_READ {
		t.Errorf("预算与 effect 上限取租户默认值，实际 %v / %v", task.GetBudget(), task.GetEffectCeiling())
	}
	if task.GetDepth() != 0 || task.GetParentTaskId() != "" {
		t.Error("从 Task API 提交的是顶层 Task")
	}
}

func TestSubmitDefaultsUnspecifiedAccessAndPriority(t *testing.T) {
	f := newAPI(t)
	f.mustSubmit(f.token("t1", "u1"), newRequest("c1"))
	_, sig := f.lastSubmit()
	if got := sig.GetTask().GetSessionAccess(); got != commonv1.SessionAccess_SESSION_ACCESS_READ_WRITE {
		t.Errorf("未声明 session_access 时按会写 Session 状态处理（串行），实际 %v", got)
	}
	if got := sig.GetTask().GetPriority(); got != commonv1.Priority_PRIORITY_INTERACTIVE {
		t.Errorf("未声明 priority 时按交互式处理，实际 %v", got)
	}
}

func TestSubmitIntoExistingSession(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	first := f.mustSubmit(tok, newRequest("c1"))
	req := newRequest("c2")
	req.SessionId = first.GetSessionId()
	second := f.mustSubmit(tok, req)
	if second.GetSessionId() != first.GetSessionId() || second.GetTaskId() == first.GetTaskId() {
		t.Errorf("同一 Session 里的第二个 Task 应有新的 task_id：%v / %v", first, second)
	}
	call, _ := f.lastSubmit()
	if call.WorkflowID != first.GetSessionId() {
		t.Errorf("应投递给已有的 Session 工作流，实际 %q", call.WorkflowID)
	}
}

func TestDuplicateSubmitReturnsTheSameTask(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	first := f.mustSubmit(tok, newRequest("c1"))
	// 新 Session 的重复提交：同一个 client_request_id、session_id 为空。
	again := f.mustSubmit(tok, newRequest("c1"))
	if again.GetTaskId() != first.GetTaskId() || again.GetSessionId() != first.GetSessionId() || !again.GetDeduplicated() {
		t.Errorf("新建 Session 的重复提交应返回原来的 Session 与 Task，并标记 deduplicated：%v / %v", first, again)
	}
	if n := len(f.durable.callsOf("SignalWithStart")); n != 1 {
		t.Errorf("信号已送达的重复提交不必再投递，实际投递 %d 次", n)
	}

	// 已有 Session 里的重复提交。
	inSession := newRequest("c2")
	inSession.SessionId = first.GetSessionId()
	a := f.mustSubmit(tok, inSession)
	b := f.mustSubmit(tok, inSession)
	if a.GetTaskId() != b.GetTaskId() || a.GetDeduplicated() || !b.GetDeduplicated() {
		t.Errorf("同一 Session 内 client_request_id 相同的重复提交应返回同一个 task_id：%v / %v", a, b)
	}
}

func TestDuplicateSubmitResendsWhenTheFirstSignalFailed(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	f.durable.faults.FailNext("SignalWithStart", providererr.Transient)
	_, err := f.submit(tok, newRequest("c1"))
	wantCode(t, "DurableEngine 暂时不可用", err, connect.CodeUnavailable)

	retry := f.mustSubmit(tok, newRequest("c1"))
	calls := f.durable.callsOf("SignalWithStart")
	if len(calls) != 2 {
		t.Fatalf("第一次没送达，重试时必须再投递，实际投递 %d 次", len(calls))
	}
	firstSig := calls[0].Payload.(*workflowv1.SubmitTaskSignal)
	if retry.GetTaskId() != firstSig.GetTask().GetTaskId() {
		t.Error("重试应沿用第一次分配的 task_id，Session 工作流按 client_request_id 去重")
	}
}

func TestSubmitToUnknownOrForeignSessionIsNotFound(t *testing.T) {
	f := newAPI(t)
	owned := f.mustSubmit(f.token("t1", "u1"), newRequest("c1"))
	before := len(f.durable.callsOf("SignalWithStart"))

	req := newRequest("c2")
	req.SessionId = owned.GetSessionId()
	_, err := f.submit(f.token("t2", "u2"), req)
	wantCode(t, "往别的租户的 Session 里提交", err, connect.CodeNotFound)

	req.SessionId = "ses_01J" + strings.Repeat("0", 22) + "Z"
	_, err = f.submit(f.token("t1", "u1"), req)
	wantCode(t, "不存在的 Session", err, connect.CodeNotFound)

	req.SessionId = "session-1"
	_, err = f.submit(f.token("t1", "u1"), req)
	wantCode(t, "格式不对的 session_id", err, connect.CodeInvalidArgument)

	if len(f.durable.callsOf("SignalWithStart")) != before {
		t.Error("这些请求都不能触达 DurableEngine")
	}
}

func TestSubmitValidatesTheRequest(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	cases := map[string]struct {
		mutate func(*taskapiv1.SubmitTaskRequest)
		code   connect.Code
	}{
		"缺少 client_request_id": {func(r *taskapiv1.SubmitTaskRequest) { r.ClientRequestId = "" }, connect.CodeInvalidArgument},
		"空命令":                  {func(r *taskapiv1.SubmitTaskRequest) { r.Message = "  " }, connect.CodeInvalidArgument},
		"附件不是 obj:// 引用":       {func(r *taskapiv1.SubmitTaskRequest) { r.Attachments = []string{"https://x/y"} }, connect.CodeInvalidArgument},
		"附件属于别的租户":             {func(r *taskapiv1.SubmitTaskRequest) { r.Attachments = []string{"obj://t2/secret.csv"} }, connect.CodePermissionDenied},
		"workspace_id 格式不对":    {func(r *taskapiv1.SubmitTaskRequest) { r.WorkspaceId = "Bad Workspace" }, connect.CodeInvalidArgument},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			req := newRequest("c-" + name)
			c.mutate(req)
			_, err := f.submit(tok, req)
			wantCode(t, name, err, c.code)
		})
	}
	if len(f.durable.callsOf("SignalWithStart")) != 0 {
		t.Error("不合法的请求不能触达 DurableEngine")
	}
}

func TestSubmitIsRateLimitedPerTenant(t *testing.T) {
	f := newAPI(t, func(o *localingress.Options) { o.Limit, o.Window = 2, time.Minute })
	t1 := f.token("t1", "u1")
	f.mustSubmit(t1, newRequest("c1"))
	f.mustSubmit(t1, newRequest("c2"))
	_, err := f.submit(t1, newRequest("c3"))
	wantCode(t, "超过租户限流", err, connect.CodeResourceExhausted)
	var ce *connect.Error
	if errors.As(err, &ce) && ce.Meta().Get("Retry-After") == "" {
		t.Error("限流应带 Retry-After")
	}
	f.mustSubmit(f.token("t2", "u2"), newRequest("c1")) // 租户之间互不影响
}

// --- 取消、信号、查询 ---

func TestCancelIsDeliveredToTheSessionWorkflow(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	sub := f.mustSubmit(tok, newRequest("c1"))
	_, err := f.client.CancelTask(context.Background(), authed(tok, &taskapiv1.CancelTaskRequest{TaskId: sub.GetTaskId(), Reason: "不需要了"}))
	if err != nil {
		t.Fatal(err)
	}
	calls := f.durable.callsOf("Signal")
	if len(calls) != 1 {
		t.Fatalf("取消应转为一个信号，实际 %d 个", len(calls))
	}
	c := calls[0]
	want := &workflowv1.CancelTaskSignal{TaskId: sub.GetTaskId(), Reason: "不需要了"}
	if c.Scope != "t1" || c.WorkflowID != sub.GetSessionId() || c.Name != session.SignalCancelTask || !proto.Equal(c.Payload, want) {
		t.Errorf("取消应以 cancel_task 信号投给 Task 所在的 Session 工作流（排队中的由它移出，运行中的由它取消），实际 %+v", c)
	}
}

func TestControlRequestsForForeignTasksAreNotFound(t *testing.T) {
	f := newAPI(t)
	sub := f.mustSubmit(f.token("t1", "u1"), newRequest("c1"))
	other := f.token("t2", "u2")
	ctx := context.Background()

	_, err := f.client.CancelTask(ctx, authed(other, &taskapiv1.CancelTaskRequest{TaskId: sub.GetTaskId()}))
	wantCode(t, "取消别的租户的 Task", err, connect.CodeNotFound)
	_, err = f.client.QueryTask(ctx, authed(other, &taskapiv1.QueryTaskRequest{TaskId: sub.GetTaskId()}))
	wantCode(t, "查询别的租户的 Task", err, connect.CodeNotFound)
	_, err = f.client.SignalTask(ctx, authed(other, &taskapiv1.SignalTaskRequest{
		TaskId: sub.GetTaskId(),
		Signal: &taskapiv1.SignalTaskRequest_HumanReply{HumanReply: &taskapiv1.HumanReply{QuestionId: "q1", Option: "continue_as_is"}},
	}))
	wantCode(t, "给别的租户的 Task 发信号", err, connect.CodeNotFound)

	for _, op := range []string{"Signal", "Query", "Cancel"} {
		for _, c := range f.durable.callsOf(op) {
			if c.Scope == "t2" {
				t.Errorf("别的租户的请求不能触达 DurableEngine：%+v", c)
			}
		}
	}
}

func TestHumanReplyIsForwardedToTheTaskWorkflow(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u7")
	sub := f.mustSubmit(tok, newRequest("c1"))
	f.durable.addWorkflow(sub.GetTaskId(), "t1", session.TaskWorkflowName, durable.StatusRunning, nil)
	_, err := f.client.SignalTask(context.Background(), authed(tok, &taskapiv1.SignalTaskRequest{
		TaskId: sub.GetTaskId(),
		Signal: &taskapiv1.SignalTaskRequest_HumanReply{HumanReply: &taskapiv1.HumanReply{
			QuestionId: "q1", Option: "replan_with_guidance", Guidance: "只看华东",
		}},
	}))
	if err != nil {
		t.Fatal(err)
	}
	calls := f.durable.callsOf("Signal")
	if len(calls) != 1 {
		t.Fatalf("人工回复应转为一个信号，实际 %d 个", len(calls))
	}
	c := calls[0]
	reply, ok := c.Payload.(*workflowv1.HumanReplySignal)
	if !ok || c.WorkflowID != sub.GetTaskId() || c.Name != "human_reply" {
		t.Fatalf("人工回复应以 human_reply 信号投给 Task 工作流，实际 %+v", c)
	}
	if reply.GetQuestionId() != "q1" || reply.GetOption() != "replan_with_guidance" || reply.GetGuidance() != "只看华东" ||
		reply.GetRepliedByUserId() != "u7" || !reply.GetRepliedAt().AsTime().Equal(f.clock.Now()) {
		t.Errorf("回复人取认证身份、时间取服务端时钟，实际 %v", reply)
	}
}

func TestSignalTaskValidation(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	sub := f.mustSubmit(tok, newRequest("c1"))
	ctx := context.Background()

	_, err := f.client.SignalTask(ctx, authed(tok, &taskapiv1.SignalTaskRequest{TaskId: sub.GetTaskId()}))
	wantCode(t, "没有信号内容", err, connect.CodeInvalidArgument)

	// Task 还在排队，子工作流尚未启动。
	_, err = f.client.SignalTask(ctx, authed(tok, &taskapiv1.SignalTaskRequest{
		TaskId: sub.GetTaskId(),
		Signal: &taskapiv1.SignalTaskRequest_HumanReply{HumanReply: &taskapiv1.HumanReply{QuestionId: "q1", Option: "continue_as_is"}},
	}))
	wantCode(t, "给还在排队的 Task 发人工回复", err, connect.CodeFailedPrecondition)
}

func TestQueryQueuedTask(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	sub := f.mustSubmit(tok, newRequest("c1"))
	resp, err := f.client.QueryTask(context.Background(), authed(tok, &taskapiv1.QueryTaskRequest{TaskId: sub.GetTaskId()}))
	if err != nil {
		t.Fatal(err)
	}
	got := resp.Msg
	if got.GetTaskId() != sub.GetTaskId() || got.GetSessionId() != sub.GetSessionId() ||
		got.GetStatus() != commonv1.TaskStatus_TASK_STATUS_QUEUED || got.GetRoundSeq() != 0 || got.GetTaskState() != nil {
		t.Errorf("子工作流尚未启动的 Task 应为 QUEUED、round_seq 为 0、没有 Task State，实际 %v", got)
	}
}

func TestQueryRunningTaskReadsTaskStateFromTheWorkflow(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	sub := f.mustSubmit(tok, newRequest("c1"))
	state, _ := structpb.NewValue(map[string]any{"task_id": sub.GetTaskId(), "goal": "g", "done_when": "d"})
	f.durable.addWorkflow(sub.GetTaskId(), "t1", session.TaskWorkflowName, durable.StatusRunning, map[string]proto.Message{
		"task_state": &workflowv1.TaskStateQueryResult{
			TaskId: sub.GetTaskId(), Status: commonv1.TaskStatus_TASK_STATUS_RUNNING, RoundSeq: 2,
			TaskState: &commonv1.JsonPayload{SchemaId: "https://agentruntime.example/schemas/task_state.schema.json", Value: state},
		},
	})
	resp, err := f.client.QueryTask(context.Background(), authed(tok, &taskapiv1.QueryTaskRequest{TaskId: sub.GetTaskId()}))
	if err != nil {
		t.Fatal(err)
	}
	got := resp.Msg
	if got.GetStatus() != commonv1.TaskStatus_TASK_STATUS_RUNNING || got.GetRoundSeq() != 2 ||
		got.GetTaskState().GetValue().GetStructValue().GetFields()["goal"].GetStringValue() != "g" {
		t.Errorf("运行中的 Task 应读取工作流内的 Task State，实际 %v", got)
	}
	queries := f.durable.callsOf("Query")
	if len(queries) == 0 || queries[len(queries)-1].Name != "task_state" || queries[len(queries)-1].WorkflowID != sub.GetTaskId() {
		t.Errorf("应以 task_id 查询 Task 工作流的 task_state，实际 %+v", queries)
	}
}

func TestQueryFinishedTaskUsesTheRecordedOutcome(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	sub := f.mustSubmit(tok, newRequest("c1"))
	// Session 工作流的 FinishTask activity 记下终态；排队中被取消的 Task 没有子工作流，只能靠它。
	acts := &session.Activities{Cache: memcache.New(memcache.Options{Clock: f.clock}), Store: f.store, Clock: f.clock, Retention: time.Hour}
	tenant := &commonv1.TenantContext{TenantId: "t1", UserId: "u1", SessionId: sub.GetSessionId()}
	if err := acts.FinishTask(context.Background(), tenant, &workflowv1.TaskWorkflowResult{
		TaskId: sub.GetTaskId(), Status: commonv1.TaskStatus_TASK_STATUS_CANCELLED,
	}); err != nil {
		t.Fatal(err)
	}
	resp, err := f.client.QueryTask(context.Background(), authed(tok, &taskapiv1.QueryTaskRequest{TaskId: sub.GetTaskId()}))
	if err != nil {
		t.Fatal(err)
	}
	if resp.Msg.GetStatus() != commonv1.TaskStatus_TASK_STATUS_CANCELLED || resp.Msg.GetLastEventId() != 1 {
		t.Errorf("已结束的 Task 应返回记下的终态与最后的 event_id，实际 %v", resp.Msg)
	}
}

func TestUnknownTaskIsNotFound(t *testing.T) {
	f := newAPI(t)
	_, err := f.client.QueryTask(context.Background(), authed(f.token("t1", "u1"), &taskapiv1.QueryTaskRequest{TaskId: "tsk_01J" + strings.Repeat("0", 23)}))
	wantCode(t, "不存在的 Task", err, connect.CodeNotFound)
	_, err = f.client.QueryTask(context.Background(), authed(f.token("t1", "u1"), &taskapiv1.QueryTaskRequest{TaskId: "../etc"}))
	wantCode(t, "格式不对的 task_id", err, connect.CodeInvalidArgument)
}

// --- 传输与错误码 ---

func TestHTTPJSONTransport(t *testing.T) {
	f := newAPI(t)
	body := []byte(`{"client_request_id":"c1","message":"你好"}`)
	req, _ := http.NewRequest(http.MethodPost, f.url+taskapiv1connect.TaskServiceSubmitTaskProcedure, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+f.token("t1", "u1"))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("HTTP + JSON 提交应成功，实际 %d：%s", resp.StatusCode, data)
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil || !taskPattern.MatchString(out["taskId"].(string)) {
		t.Errorf("JSON 响应应带 taskId，实际 %s", data)
	}
}

func TestConnectErrorMapping(t *testing.T) {
	rate := providererr.New(providererr.Transient, "rate_limit", "请求过于频繁")
	rate.RetryAfter = 3 * time.Second
	cases := []struct {
		err  error
		code connect.Code
	}{
		{providererr.New(providererr.Invalid, "invalid_argument", "x"), connect.CodeInvalidArgument},
		{providererr.New(providererr.Denied, "unauthenticated", "x"), connect.CodeUnauthenticated},
		{providererr.New(providererr.Denied, "tenant_mismatch", "x"), connect.CodePermissionDenied},
		{providererr.New(providererr.NotFound, "not_found", "x"), connect.CodeNotFound},
		{providererr.New(providererr.Conflict, "id_in_use", "x"), connect.CodeAlreadyExists},
		{providererr.New(providererr.Transient, "unavailable", "x"), connect.CodeUnavailable},
		{rate, connect.CodeResourceExhausted},
		{errors.New("unclassified"), connect.CodeInternal},
	}
	for _, c := range cases {
		got := ConnectError(c.err)
		if got.Code() != c.code {
			t.Errorf("%v 应映射为 %v，实际 %v", c.err, c.code, got.Code())
		}
	}
	if ConnectError(rate).Meta().Get("Retry-After") != "3" {
		t.Error("限流错误应带 Retry-After（秒）")
	}
	// 说明是中文；应用按 ErrorInfo 里稳定的 code 翻译。
	denied := ConnectError(providererr.New(providererr.Denied, "network_not_allowed", "租户策略不允许沙箱联网"))
	var reasons []string
	for _, d := range denied.Details() {
		if v, err := d.Value(); err == nil {
			if info, ok := v.(*errdetails.ErrorInfo); ok && info.GetDomain() == ErrorDomain {
				reasons = append(reasons, info.GetReason())
			}
		}
	}
	if !slices.Equal(reasons, []string{"network_not_allowed"}) {
		t.Errorf("错误应带 ErrorInfo{reason: network_not_allowed}，实际 %v", reasons)
	}
	if len(ConnectError(errors.New("unclassified")).Details()) != 0 {
		t.Error("没分类的错误不带 code")
	}
}
