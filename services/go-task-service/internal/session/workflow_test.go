package session

// S-03 验收：Session 工作流（设计书 3.1.1、附录 K.1；计划 S-03 难点“Continue-As-New”“子工作流 ID”“上限”“取消”）。
//
// 用 Temporal 的测试环境跑：时间可快进，Task 子工作流用 fakeTask 代替 Python 的 Task 工作流，
// AcceptTask、FinishTask 两个 activity 用 mock 记录调用。测试环境不支持子工作流给父工作流发信号，
// 所以 task_finished 信号由用例直接发送，模拟 Python 侧的通知。

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/mock"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"

	temporaladapter "github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

var testSession = "ses_01J" + strings.Repeat("0", 22) + "S"

func testTenant() *commonv1.TenantContext {
	return &commonv1.TenantContext{TenantId: "t1", UserId: "u1", SessionId: testSession}
}

// taskID 生成合乎格式的 task_id，字典序与 n 的大小一致。
func taskID(n int) string { return fmt.Sprintf("tsk_01J%023d", n) }

const (
	write    = commonv1.SessionAccess_SESSION_ACCESS_READ_WRITE
	readOnly = commonv1.SessionAccess_SESSION_ACCESS_READ_ONLY
)

// submit 构造一个提交信号。holdMinutes 是 fakeTask 运行的时长；outcome 为 "fail" 时 fakeTask 失败。
func submit(n int, crid string, access commonv1.SessionAccess, holdMinutes float64, outcome string) *workflowv1.SubmitTaskSignal {
	inputs, _ := structpb.NewStruct(map[string]any{"hold_minutes": holdMinutes, "outcome": outcome})
	return &workflowv1.SubmitTaskSignal{
		ClientRequestId: crid,
		Task: &workflowv1.TaskWorkflowInput{
			Tenant:         testTenant(),
			TaskId:         taskID(n),
			Message:        fmt.Sprintf("task %d", n),
			Inputs:         inputs,
			SessionAccess:  access,
			PinnedVersions: &workflowv1.PinnedVersions{Recipe: "stub@v1", NodeTemplates: map[string]string{"stub_answer": "v1"}},
		},
	}
}

func newInput() *workflowv1.SessionWorkflowInput {
	return &workflowv1.SessionWorkflowInput{Tenant: testTenant()}
}

// fakeTask 代替 Python 的 Task 工作流：运行 hold_minutes 分钟后判 done；outcome 为 "fail" 时失败；被取消时返回取消错误。
func fakeTask(ctx workflow.Context, in *workflowv1.TaskWorkflowInput) (*workflowv1.TaskWorkflowResult, error) {
	fields := in.GetInputs().GetFields()
	hold := time.Duration(fields["hold_minutes"].GetNumberValue() * float64(time.Minute))
	if err := workflow.Sleep(ctx, hold); err != nil {
		return nil, err
	}
	if fields["outcome"].GetStringValue() == "fail" {
		return nil, temporal.NewNonRetryableApplicationError("stub failure", "stub", nil)
	}
	return &workflowv1.TaskWorkflowResult{
		TaskId:       in.GetTaskId(),
		Status:       commonv1.TaskStatus_TASK_STATUS_DONE,
		FinalVerdict: commonv1.Verdict_VERDICT_DONE,
		Rounds:       1,
	}, nil
}

type harness struct {
	t   *testing.T
	env *testsuite.TestWorkflowEnvironment

	mu         sync.Mutex
	log        []string // accept:<id>、start:<id>、done:<id>、finish:<id>
	startedAt  map[string]time.Time
	doneAt     map[string]time.Time
	childInput map[string]*workflowv1.TaskWorkflowInput
	finishes   map[string][]*workflowv1.TaskWorkflowResult
	accepts    map[string]int
	tenants    []*commonv1.TenantContext
}

func newHarness(t *testing.T, cfg Config) *harness {
	t.Helper()
	var suite testsuite.WorkflowTestSuite
	env := suite.NewTestWorkflowEnvironment()
	h := &harness{
		t: t, env: env,
		startedAt:  map[string]time.Time{},
		doneAt:     map[string]time.Time{},
		childInput: map[string]*workflowv1.TaskWorkflowInput{},
		finishes:   map[string][]*workflowv1.TaskWorkflowResult{},
		accepts:    map[string]int{},
	}
	env.RegisterWorkflowWithOptions(NewWorkflow(cfg), workflow.RegisterOptions{Name: WorkflowName})
	env.RegisterWorkflowWithOptions(fakeTask, workflow.RegisterOptions{Name: TaskWorkflowName})
	acts := &Activities{}
	env.RegisterActivity(acts)
	env.OnActivity(acts.AcceptTask, mock.Anything, mock.Anything, mock.Anything).Return(
		func(_ context.Context, tenant *commonv1.TenantContext, id string) error {
			h.mu.Lock()
			defer h.mu.Unlock()
			h.log = append(h.log, "accept:"+id)
			h.accepts[id]++
			h.tenants = append(h.tenants, tenant)
			return nil
		})
	env.OnActivity(acts.FinishTask, mock.Anything, mock.Anything, mock.Anything).Return(
		func(_ context.Context, tenant *commonv1.TenantContext, r *workflowv1.TaskWorkflowResult) error {
			h.mu.Lock()
			defer h.mu.Unlock()
			h.log = append(h.log, "finish:"+r.GetTaskId())
			h.finishes[r.GetTaskId()] = append(h.finishes[r.GetTaskId()], r)
			h.tenants = append(h.tenants, tenant)
			return nil
		})
	env.SetOnChildWorkflowStartedListener(func(info *workflow.Info, _ workflow.Context, args converter.EncodedValues) {
		h.mu.Lock()
		defer h.mu.Unlock()
		id := info.WorkflowExecution.ID
		h.log = append(h.log, "start:"+id)
		h.startedAt[id] = env.Now()
		in := &workflowv1.TaskWorkflowInput{}
		if err := args.Get(in); err != nil {
			t.Errorf("子工作流 %s 的输入无法解码：%v", id, err)
		}
		h.childInput[id] = in
	})
	childDone := func(id string) {
		h.mu.Lock()
		defer h.mu.Unlock()
		if _, ok := h.doneAt[id]; !ok {
			h.log = append(h.log, "done:"+id)
			h.doneAt[id] = env.Now()
		}
	}
	env.SetOnChildWorkflowCompletedListener(func(info *workflow.Info, _ converter.EncodedValue, _ error) {
		childDone(info.WorkflowExecution.ID)
	})
	env.SetOnChildWorkflowCanceledListener(func(info *workflow.Info) { childDone(info.WorkflowExecution.ID) })
	return h
}

// at 在测试环境的第 d 时刻执行 fn。
func (h *harness) at(d time.Duration, fn func()) { h.env.RegisterDelayedCallback(fn, d) }

func (h *harness) signal(name string, payload proto.Message) { h.env.SignalWorkflow(name, payload) }

func (h *harness) submit(sigs ...*workflowv1.SubmitTaskSignal) {
	for _, s := range sigs {
		h.signal(SignalSubmitTask, s)
	}
}

func (h *harness) cancel(n int) {
	h.signal(SignalCancelTask, &workflowv1.CancelTaskSignal{TaskId: taskID(n), Reason: "user"})
}

func (h *harness) finished(n int, status commonv1.TaskStatus) {
	h.signal(SignalTaskFinished, &workflowv1.TaskWorkflowResult{TaskId: taskID(n), Status: status})
}

func (h *harness) state() *workflowv1.SessionState {
	h.t.Helper()
	v, err := h.env.QueryWorkflow(QuerySessionState)
	if err != nil {
		h.t.Errorf("session_state 查询失败：%v", err)
		return &workflowv1.SessionState{}
	}
	st := &workflowv1.SessionState{}
	if err := v.Get(st); err != nil {
		h.t.Errorf("session_state 结果无法解码：%v", err)
	}
	return st
}

// run 执行 Session 工作流，到 until 时刻取消它（Session 工作流本身不会结束）。
func (h *harness) run(in *workflowv1.SessionWorkflowInput, until time.Duration) {
	h.t.Helper()
	h.at(until, h.env.CancelWorkflow)
	h.env.ExecuteWorkflow(WorkflowName, in)
	if !h.env.IsWorkflowCompleted() {
		h.t.Fatal("Session 工作流没有结束")
	}
}

// runExpectingCancel 执行到 until 时刻取消，并要求工作流因取消而结束，而不是出错或续接。
func (h *harness) runExpectingCancel(in *workflowv1.SessionWorkflowInput, until time.Duration) {
	h.t.Helper()
	h.run(in, until)
	if err := h.env.GetWorkflowError(); !temporal.IsCanceledError(err) {
		h.t.Fatalf("Session 工作流应当因取消而结束，实际：%v", err)
	}
}

// runExpectingContinueAsNew 执行并要求工作流续接，返回续接的输入。
func (h *harness) runExpectingContinueAsNew(in *workflowv1.SessionWorkflowInput, until time.Duration) *workflowv1.SessionWorkflowInput {
	h.t.Helper()
	h.run(in, until)
	var can *workflow.ContinueAsNewError
	if err := h.env.GetWorkflowError(); !errors.As(err, &can) {
		h.t.Fatalf("Session 工作流应当 Continue-As-New，实际：%v", err)
	}
	if can.WorkflowType == nil || can.WorkflowType.Name != WorkflowName {
		h.t.Fatalf("续接的工作流类型应为 %s，实际 %v", WorkflowName, can.WorkflowType)
	}
	next := &workflowv1.SessionWorkflowInput{}
	if err := converter.GetDefaultDataConverter().FromPayloads(can.Input, next); err != nil {
		h.t.Fatalf("续接的输入无法解码：%v", err)
	}
	return next
}

func (h *harness) startOrder() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	var ids []string
	for _, e := range h.log {
		if id, ok := strings.CutPrefix(e, "start:"); ok {
			ids = append(ids, id)
		}
	}
	return ids
}

func (h *harness) finishedOnce(n int, want commonv1.TaskStatus) *workflowv1.TaskWorkflowResult {
	h.t.Helper()
	h.mu.Lock()
	defer h.mu.Unlock()
	got := h.finishes[taskID(n)]
	if len(got) != 1 {
		h.t.Fatalf("Task %d 的 FinishTask 应当恰好调用一次，实际 %d 次", n, len(got))
	}
	if got[0].GetStatus() != want {
		h.t.Fatalf("Task %d 的终态应为 %v，实际 %v", n, want, got[0].GetStatus())
	}
	return got[0]
}

func ids(ns ...int) []string {
	out := make([]string, len(ns))
	for i, n := range ns {
		out[i] = taskID(n)
	}
	return out
}

func eq[T comparable](t *testing.T, what string, got, want []T) {
	t.Helper()
	if !slices.Equal(got, want) {
		t.Errorf("%s：期望 %v，实际 %v", what, want, got)
	}
}

func defaultCfg() Config { return DefaultConfig() }

// --- 写 Task 串行、只读 Task 并行 ---

func TestWriteTasksRunOneAtATime(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() {
		h.submit(submit(1, "c1", write, 30, ""), submit(2, "c2", write, 30, ""), submit(3, "c3", write, 0, ""))
	})
	h.at(10*time.Minute, func() {
		st := h.state()
		if st.GetRunningWriteTaskId() != taskID(1) {
			t.Errorf("10 分钟时运行中的写 Task 应为 1，实际 %q", st.GetRunningWriteTaskId())
		}
		eq(t, "10 分钟时排队的 Task", st.GetQueuedTaskIds(), ids(2, 3))
	})
	h.at(40*time.Minute, func() {
		st := h.state()
		if st.GetRunningWriteTaskId() != taskID(2) {
			t.Errorf("40 分钟时运行中的写 Task 应为 2，实际 %q", st.GetRunningWriteTaskId())
		}
		eq(t, "40 分钟时排队的 Task", st.GetQueuedTaskIds(), ids(3))
	})
	h.at(2*time.Hour, func() {
		if st := h.state(); st.GetRunningWriteTaskId() != "" || len(st.GetQueuedTaskIds()) != 0 || st.GetAcceptedTasks() != 3 {
			t.Errorf("全部结束后不应再有运行中或排队的 Task：%v", st)
		}
	})
	h.runExpectingCancel(newInput(), 3*time.Hour)

	eq(t, "子工作流启动顺序", h.startOrder(), ids(1, 2, 3))
	for _, pair := range [][2]int{{1, 2}, {2, 3}} {
		prev, next := taskID(pair[0]), taskID(pair[1])
		if h.startedAt[next].Before(h.doneAt[prev]) {
			t.Errorf("写 Task %s 在 %s 结束前就启动了", next, prev)
		}
	}
	for n := 1; n <= 3; n++ {
		h.finishedOnce(n, commonv1.TaskStatus_TASK_STATUS_DONE)
	}
}

func TestReadOnlyTasksRunInParallelUpToTheLimit(t *testing.T) {
	cfg := defaultCfg()
	cfg.ReadOnlyLimit = 2
	h := newHarness(t, cfg)
	h.at(time.Minute, func() {
		h.submit(
			submit(1, "c1", write, 60, ""),
			submit(2, "c2", readOnly, 30, ""),
			submit(3, "c3", readOnly, 30, ""),
			submit(4, "c4", readOnly, 30, ""),
		)
	})
	h.at(10*time.Minute, func() {
		st := h.state()
		if st.GetRunningWriteTaskId() != taskID(1) {
			t.Errorf("写 Task 1 应在运行，实际 %q", st.GetRunningWriteTaskId())
		}
		eq(t, "10 分钟时运行中的只读 Task", st.GetRunningReadOnlyTaskIds(), ids(2, 3))
		eq(t, "10 分钟时排队的 Task（只读 Task 等并行名额）", st.GetQueuedTaskIds(), ids(4))
	})
	h.at(40*time.Minute, func() {
		st := h.state()
		eq(t, "40 分钟时运行中的只读 Task", st.GetRunningReadOnlyTaskIds(), ids(4))
		if st.GetRunningWriteTaskId() != taskID(1) {
			t.Errorf("40 分钟时写 Task 1 应仍在运行")
		}
	})
	h.runExpectingCancel(newInput(), 3*time.Hour)

	for _, n := range []int{2, 3} {
		if !h.startedAt[taskID(n)].Equal(h.startedAt[taskID(1)]) {
			t.Errorf("只读 Task %d 应与写 Task 1 同时启动，不等写 Task 结束", n)
		}
	}
	first := h.doneAt[taskID(2)]
	if d := h.doneAt[taskID(3)]; d.Before(first) {
		first = d
	}
	if h.startedAt[taskID(4)].Before(first) {
		t.Error("超出上限的只读 Task 4 在有名额之前就启动了")
	}
	for n := 1; n <= 4; n++ {
		h.finishedOnce(n, commonv1.TaskStatus_TASK_STATUS_DONE)
	}
}

func TestUnspecifiedSessionAccessIsTreatedAsWrite(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() {
		h.submit(
			submit(1, "c1", commonv1.SessionAccess_SESSION_ACCESS_UNSPECIFIED, 30, ""),
			submit(2, "c2", write, 30, ""),
		)
	})
	h.at(10*time.Minute, func() {
		st := h.state()
		if st.GetRunningWriteTaskId() != taskID(1) {
			t.Errorf("未声明 session_access 的 Task 应按写 Task 串行，实际运行中的写 Task 为 %q", st.GetRunningWriteTaskId())
		}
		eq(t, "排队的 Task", st.GetQueuedTaskIds(), ids(2))
	})
	h.runExpectingCancel(newInput(), 2*time.Hour)
}

// --- 子工作流 ---

func TestChildOptions(t *testing.T) {
	task := submit(7, "c7", write, 0, "").GetTask()
	opts, err := childOptions(task)
	if err != nil {
		t.Fatal(err)
	}
	if opts.WorkflowID != taskID(7) {
		t.Errorf("子工作流 ID 应为 task_id，实际 %q", opts.WorkflowID)
	}
	if opts.TaskQueue != TaskTaskQueue {
		t.Errorf("子工作流应投到 %q 队列，实际 %q", TaskTaskQueue, opts.TaskQueue)
	}
	if opts.ParentClosePolicy != enumspb.PARENT_CLOSE_POLICY_ABANDON {
		t.Errorf("父工作流续接时子工作流必须继续运行（ABANDON），实际 %v", opts.ParentClosePolicy)
	}
	if opts.WorkflowIDReusePolicy != enumspb.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE {
		t.Errorf("同一 task_id 不能再跑第二次（REJECT_DUPLICATE），实际 %v", opts.WorkflowIDReusePolicy)
	}
	if got := opts.Memo[temporaladapter.ScopeMemoKey]; got != "t1" {
		t.Errorf("子工作流的 memo 应记下所属租户 t1，实际 %v", got)
	}

	for name, mutate := range map[string]func(*workflowv1.TaskWorkflowInput){
		"缺少租户":       func(in *workflowv1.TaskWorkflowInput) { in.Tenant = nil },
		"缺少 task_id": func(in *workflowv1.TaskWorkflowInput) { in.TaskId = "" },
	} {
		bad := proto.Clone(task).(*workflowv1.TaskWorkflowInput)
		mutate(bad)
		if _, err := childOptions(bad); err == nil {
			t.Errorf("%s 时 childOptions 应当报错", name)
		}
	}
}

func TestChildReceivesTheSubmittedTaskUnchanged(t *testing.T) {
	h := newHarness(t, defaultCfg())
	sig := submit(1, "c1", write, 0, "")
	h.at(time.Minute, func() { h.submit(sig) })
	h.runExpectingCancel(newInput(), time.Hour)
	got := h.childInput[taskID(1)]
	if got == nil || !proto.Equal(got, sig.GetTask()) {
		t.Errorf("子工作流的输入应与提交的 TaskWorkflowInput 完全一致（含固定的版本）\n期望 %v\n实际 %v", sig.GetTask(), got)
	}
}

func TestDuplicateSubmitStartsOneTask(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() {
		h.submit(submit(1, "c1", write, 10, ""), submit(1, "c1", write, 10, ""))
		// 同一 client_request_id 带着另一个 task_id：仍按重复处理，以第一次为准。
		h.submit(submit(9, "c1", write, 10, ""))
	})
	h.at(5*time.Minute, func() {
		if got := h.state().GetAcceptedTasks(); got != 1 {
			t.Errorf("重复提交不应计数，accepted_tasks 应为 1，实际 %d", got)
		}
	})
	h.runExpectingCancel(newInput(), time.Hour)
	eq(t, "子工作流", h.startOrder(), ids(1))
	if h.accepts[taskID(1)] != 1 || h.accepts[taskID(9)] != 0 {
		t.Errorf("AcceptTask 应只为 Task 1 调用一次，实际 %v", h.accepts)
	}
}

func TestAcceptedEventPrecedesChildStart(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() {
		h.submit(submit(1, "c1", write, 5, ""), submit(2, "c2", readOnly, 5, ""), submit(3, "c3", write, 5, ""))
	})
	h.runExpectingCancel(newInput(), time.Hour)
	for n := 1; n <= 3; n++ {
		id := taskID(n)
		accept, start := slices.Index(h.log, "accept:"+id), slices.Index(h.log, "start:"+id)
		if accept < 0 || start < 0 || accept > start {
			t.Errorf("Task %d：AcceptTask 必须先于子工作流启动，事件序列 %v", n, h.log)
		}
	}
	for _, tenant := range h.tenants {
		if tenant.GetTenantId() != "t1" || tenant.GetSessionId() != testSession {
			t.Errorf("activity 必须带上 Session 的租户上下文，实际 %v", tenant)
		}
	}
}

// --- 结束通知：子工作流的 future 与 task_finished 信号 ---

func TestFinishTaskRunsOnceWhenSignalArrivesBeforeCompletion(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 10, ""), submit(2, "c2", write, 10, "")) })
	// Python 的 Task 工作流先发 task_finished 再返回，信号通常先于子工作流结束到达。
	h.at(5*time.Minute, func() { h.finished(1, commonv1.TaskStatus_TASK_STATUS_DONE) })
	h.at(6*time.Minute, func() {
		if got := h.state().GetRunningWriteTaskId(); got != taskID(2) {
			t.Errorf("收到 Task 1 的结束信号后应启动下一个写 Task，实际运行中的是 %q", got)
		}
	})
	h.runExpectingCancel(newInput(), time.Hour)
	h.finishedOnce(1, commonv1.TaskStatus_TASK_STATUS_DONE)
	h.finishedOnce(2, commonv1.TaskStatus_TASK_STATUS_DONE)
}

func TestFinishTaskRunsOnceWhenSignalArrivesAfterCompletion(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 10, "")) })
	h.at(20*time.Minute, func() { h.finished(1, commonv1.TaskStatus_TASK_STATUS_DONE) })
	h.runExpectingCancel(newInput(), time.Hour)
	h.finishedOnce(1, commonv1.TaskStatus_TASK_STATUS_DONE)
}

func TestFinishedSignalForUnknownTaskIsIgnored(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() { h.finished(42, commonv1.TaskStatus_TASK_STATUS_DONE) })
	h.at(2*time.Minute, func() { h.submit(submit(1, "c1", write, 0, "")) })
	h.runExpectingCancel(newInput(), time.Hour)
	if len(h.finishes[taskID(42)]) != 0 {
		t.Error("不属于本 Session 的 Task 不应发 TASK_FINISHED")
	}
	h.finishedOnce(1, commonv1.TaskStatus_TASK_STATUS_DONE)
}

func TestFailedTaskReleasesTheWriteQueue(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 5, "fail"), submit(2, "c2", write, 5, "")) })
	h.runExpectingCancel(newInput(), time.Hour)
	failed := h.finishedOnce(1, commonv1.TaskStatus_TASK_STATUS_FAILED)
	if failed.GetError().GetMessage() == "" {
		t.Error("失败的 Task 应带上错误说明")
	}
	h.finishedOnce(2, commonv1.TaskStatus_TASK_STATUS_DONE)
}

// --- 取消 ---

func TestCancelQueuedTaskRemovesItWithoutStarting(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 30, ""), submit(2, "c2", write, 30, "")) })
	h.at(5*time.Minute, func() { h.cancel(2) })
	h.at(6*time.Minute, func() {
		if q := h.state().GetQueuedTaskIds(); len(q) != 0 {
			t.Errorf("取消后队列应为空，实际 %v", q)
		}
	})
	h.runExpectingCancel(newInput(), 2*time.Hour)
	eq(t, "子工作流", h.startOrder(), ids(1))
	h.finishedOnce(2, commonv1.TaskStatus_TASK_STATUS_CANCELLED)
	h.finishedOnce(1, commonv1.TaskStatus_TASK_STATUS_DONE)
}

func TestCancelRunningTaskCancelsTheChildAndWaitsForIt(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 30, ""), submit(2, "c2", write, 5, "")) })
	h.at(5*time.Minute, func() { h.cancel(1) })
	h.runExpectingCancel(newInput(), 2*time.Hour)
	h.finishedOnce(1, commonv1.TaskStatus_TASK_STATUS_CANCELLED)
	h.finishedOnce(2, commonv1.TaskStatus_TASK_STATUS_DONE)
	if !h.doneAt[taskID(1)].Before(h.startedAt[taskID(1)].Add(30 * time.Minute)) {
		t.Error("取消应当传到运行中的子工作流，而不是等它自然结束")
	}
	if h.startedAt[taskID(2)].Before(h.doneAt[taskID(1)]) {
		t.Error("被取消的写 Task 收尾完成之前，下一个写 Task 不应启动")
	}
}

func TestCancelUnknownTaskIsIgnored(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() { h.cancel(99) })
	h.at(2*time.Minute, func() { h.submit(submit(1, "c1", write, 0, "")) })
	h.runExpectingCancel(newInput(), time.Hour)
	h.finishedOnce(1, commonv1.TaskStatus_TASK_STATUS_DONE)
}

// --- Continue-As-New ---

func TestContinueAsNewAfterTaskThresholdCarriesStateAndDrainsSignals(t *testing.T) {
	cfg := defaultCfg()
	cfg.ContinueAsNewTasks = 3
	cfg.ReadOnlyLimit = 1
	h := newHarness(t, cfg)
	h.at(time.Minute, func() {
		h.submit(
			submit(1, "c1", write, 60, ""),
			submit(2, "c2", readOnly, 60, ""),
			submit(3, "c3", write, 60, ""), // 第 3 个被接受的 Task，达到阈值
			// 下面三个在续接时还在信号通道里，必须排空带入新的运行。
			submit(4, "c4", readOnly, 60, ""),
			submit(5, "c5", write, 60, ""),
			submit(1, "c1", write, 60, ""), // 重复提交，排空时丢弃
		)
	})
	next := h.runExpectingContinueAsNew(newInput(), 2*time.Hour)

	if next.GetRunGeneration() != 1 {
		t.Errorf("run_generation 应为 1，实际 %d", next.GetRunGeneration())
	}
	if next.GetAcceptedTasks() != 3 {
		t.Errorf("本次运行接受了 3 个 Task，实际 %d", next.GetAcceptedTasks())
	}
	if !proto.Equal(next.GetTenant(), testTenant()) {
		t.Errorf("租户上下文必须原样带入新的运行，实际 %v", next.GetTenant())
	}
	if next.GetRunningWriteTaskId() != taskID(1) {
		t.Errorf("运行中的写 Task 应带入新的运行，实际 %q", next.GetRunningWriteTaskId())
	}
	eq(t, "运行中的只读 Task", next.GetRunningReadOnlyTaskIds(), ids(2))
	var pending []string
	for _, p := range next.GetPending() {
		pending = append(pending, p.GetTask().GetTaskId())
	}
	eq(t, "pending：先是已接受的排队 Task，再是排空的信号（按到达顺序，去掉重复）", pending, ids(3, 4, 5))
	wantSubmitted := map[string]string{"c1": taskID(1), "c2": taskID(2), "c3": taskID(3)}
	if len(next.GetSubmitted()) != len(wantSubmitted) {
		t.Errorf("submitted 应只含已接受的 Task，实际 %v", next.GetSubmitted())
	}
	for crid, id := range wantSubmitted {
		if next.GetSubmitted()[crid] != id {
			t.Errorf("submitted[%s] 应为 %s，实际 %q", crid, id, next.GetSubmitted()[crid])
		}
	}
	eq(t, "子工作流（续接前只启动有名额的）", h.startOrder(), ids(1, 2))
}

func TestCancelDrainedWithItsSubmitIsAcceptedThenCancelled(t *testing.T) {
	// S-07 场景 8：续接前排空信号时，排空的提交随即被取消。与平时一样先接受（计数、去重表、TASK_ACCEPTED），
	// 再从队列里取消；不启动子工作流，也不带入新的运行（版本分支 VersionAcceptDrainedCancel）。
	cfg := defaultCfg()
	cfg.ContinueAsNewTasks = 3
	h := newHarness(t, cfg)
	h.at(time.Minute, func() {
		h.submit(
			submit(1, "c1", write, 60, ""),
			submit(2, "c2", write, 60, ""),
			submit(3, "c3", write, 60, ""), // 达到阈值
			submit(4, "c4", write, 60, ""), // 排空
		)
		h.cancel(4) // 与提交一起被排空
	})
	next := h.runExpectingContinueAsNew(newInput(), 2*time.Hour)

	if next.GetAcceptedTasks() != 4 {
		t.Errorf("排空后又被取消的 Task 也算已接受，实际 %d", next.GetAcceptedTasks())
	}
	if next.GetSubmitted()["c4"] != taskID(4) {
		t.Errorf("它的 client_request_id 要进去重表，重复投递才不会再跑一次，实际 %v", next.GetSubmitted())
	}
	for _, p := range next.GetPending() {
		if p.GetTask().GetTaskId() == taskID(4) {
			t.Error("被取消的 Task 不带入新的运行")
		}
	}
	h.mu.Lock()
	accepted := h.accepts[taskID(4)]
	h.mu.Unlock()
	if accepted != 1 {
		t.Errorf("应推送一次 TASK_ACCEPTED，实际 %d 次", accepted)
	}
	h.finishedOnce(4, commonv1.TaskStatus_TASK_STATUS_CANCELLED)
	eq(t, "子工作流", h.startOrder(), ids(1))
	idx := func(e string) int { return slices.Index(h.log, e) }
	if idx("accept:"+taskID(4)) < 0 || idx("accept:"+taskID(4)) > idx("finish:"+taskID(4)) {
		t.Errorf("先接受再取消，实际顺序 %v", h.log)
	}
}

func TestNewRunResumesFromCarriedState(t *testing.T) {
	cfg := defaultCfg()
	cfg.ReadOnlyLimit = 1
	h := newHarness(t, cfg)
	in := &workflowv1.SessionWorkflowInput{
		Tenant:                 testTenant(),
		RunningWriteTaskId:     taskID(1),
		RunningReadOnlyTaskIds: ids(2),
		Pending: []*workflowv1.SubmitTaskSignal{
			submit(3, "c3", write, 5, ""),    // 上一次运行已接受，排队中
			submit(4, "c4", readOnly, 5, ""), // 排空的信号，尚未接受
		},
		Submitted:     map[string]string{"c1": taskID(1), "c2": taskID(2), "c3": taskID(3)},
		AcceptedTasks: 3,
		RunGeneration: 1,
	}
	h.at(time.Minute, func() {
		st := h.state()
		if st.GetSessionId() != testSession || st.GetRunGeneration() != 1 || st.GetAcceptedTasks() != 4 {
			t.Errorf("新的运行应接着上一次的状态（Task 4 在本次运行接受），实际 %v", st)
		}
		if st.GetRunningWriteTaskId() != taskID(1) {
			t.Errorf("上一次运行留下的写 Task 1 仍在运行，实际 %q", st.GetRunningWriteTaskId())
		}
		eq(t, "运行中的只读 Task", st.GetRunningReadOnlyTaskIds(), ids(2))
		eq(t, "排队的 Task", st.GetQueuedTaskIds(), ids(3, 4))
	})
	// 上一次运行启动的子工作流没有 future，只能靠 task_finished 信号得知结束。
	var writeReleased time.Time
	h.at(2*time.Minute, func() { h.finished(2, commonv1.TaskStatus_TASK_STATUS_DONE) })
	h.at(3*time.Minute, func() {
		writeReleased = h.env.Now()
		h.finished(1, commonv1.TaskStatus_TASK_STATUS_DONE)
	})
	h.runExpectingCancel(in, time.Hour)

	if h.accepts[taskID(3)] != 0 {
		t.Error("上一次运行已接受的 Task 不应再发 TASK_ACCEPTED")
	}
	if h.accepts[taskID(4)] != 1 {
		t.Error("排空带入的信号应在新的运行里接受一次")
	}
	h.finishedOnce(1, commonv1.TaskStatus_TASK_STATUS_DONE)
	h.finishedOnce(2, commonv1.TaskStatus_TASK_STATUS_DONE)
	h.finishedOnce(3, commonv1.TaskStatus_TASK_STATUS_DONE)
	h.finishedOnce(4, commonv1.TaskStatus_TASK_STATUS_DONE)
	if h.startedAt[taskID(3)].Before(writeReleased) {
		t.Error("写 Task 3 应在上一次运行的写 Task 1 结束后才启动")
	}
	if !h.startedAt[taskID(4)].Before(writeReleased) {
		t.Error("只读 Task 4 只等只读名额（Task 2 结束），不等写 Task 1")
	}
}

func TestSessionStateIsTheSameAcrossContinueAsNew(t *testing.T) {
	cfg := defaultCfg()
	cfg.ReadOnlyLimit = 1
	h := newHarness(t, cfg)
	h.at(time.Minute, func() {
		h.submit(submit(1, "c1", write, 60, ""), submit(2, "c2", write, 60, ""),
			submit(3, "c3", readOnly, 60, ""), submit(4, "c4", readOnly, 60, ""))
	})
	var before *workflowv1.SessionState
	h.at(5*time.Minute, func() { before = h.state() })
	h.at(6*time.Minute, func() {
		h.env.SetContinueAsNewSuggested(true)
		h.cancel(99) // 唤醒工作流；取消不存在的 Task 不改变状态
	})
	next := h.runExpectingContinueAsNew(newInput(), 2*time.Hour)

	h2 := newHarness(t, cfg)
	var after *workflowv1.SessionState
	h2.at(time.Minute, func() { after = h2.state() })
	h2.runExpectingCancel(next, 2*time.Minute)

	if before == nil || after == nil {
		t.Fatal("没有拿到续接前后的查询结果")
	}
	want := proto.Clone(before).(*workflowv1.SessionState)
	want.RunGeneration++
	if !proto.Equal(after, want) {
		t.Errorf("续接前后 session_state 应一致（run_generation 加 1）\n续接前 %v\n续接后 %v", before, after)
	}
}

func TestContinueAsNewWhenHistoryIsLong(t *testing.T) {
	cfg := defaultCfg()
	cfg.ContinueAsNewEvents = 1000
	h := newHarness(t, cfg)
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 60, "")) })
	h.at(5*time.Minute, func() {
		h.env.SetCurrentHistoryLength(1000)
		h.submit(submit(2, "c2", write, 60, ""))
	})
	next := h.runExpectingContinueAsNew(newInput(), 2*time.Hour)
	if next.GetRunningWriteTaskId() != taskID(1) || next.GetAcceptedTasks() != 2 {
		t.Errorf("续接的输入不对：%v", next)
	}
}

func TestDedupeMemoryIsBoundedButKeepsActiveTasks(t *testing.T) {
	cfg := defaultCfg()
	cfg.DedupeLimit = 2
	h := newHarness(t, cfg)
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 0, "")) })
	h.at(2*time.Minute, func() { h.submit(submit(2, "c2", write, 0, "")) })
	h.at(3*time.Minute, func() { h.submit(submit(3, "c3", write, 0, "")) })
	h.at(4*time.Minute, func() { h.submit(submit(4, "c4", write, 60, ""), submit(5, "c5", write, 60, "")) })
	h.at(10*time.Minute, func() {
		h.env.SetContinueAsNewSuggested(true)
		h.cancel(99)
	})
	next := h.runExpectingContinueAsNew(newInput(), 2*time.Hour)
	got := next.GetSubmitted()
	if got["c4"] != taskID(4) || got["c5"] != taskID(5) {
		t.Errorf("运行中与排队中的 Task 不能被淘汰出去重表，实际 %v", got)
	}
	if _, ok := got["c1"]; ok {
		t.Errorf("最早结束的 Task 应先被淘汰，实际 %v", got)
	}
	if len(got) > 2 {
		t.Errorf("去重表超过上限 2（且没有超过 2 个未结束的 Task）：%v", got)
	}
}

func TestContinueAsNewWhenSuggested(t *testing.T) {
	h := newHarness(t, defaultCfg())
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 60, "")) })
	h.at(5*time.Minute, func() {
		h.env.SetContinueAsNewSuggested(true)
		h.submit(submit(2, "c2", readOnly, 60, ""))
	})
	next := h.runExpectingContinueAsNew(newInput(), 2*time.Hour)
	if next.GetRunningWriteTaskId() != taskID(1) || !slices.Equal(next.GetRunningReadOnlyTaskIds(), ids(2)) {
		t.Errorf("续接的输入应带上运行中的 Task：%v", next)
	}
}
