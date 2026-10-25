package session

// 夜间任务 2026-10-20、10-22 的 UNHANDLED_COMMAND（tests/e2e 的 test_duplicate_task_finished_does_not_release_the_write_lock_twice）：
// Session 工作流决定 Continue-As-New 的那个工作流任务还没提交时又来了信号，服务端以 UNHANDLED_COMMAND 拒绝续接命令，
// 工作流带着新信号重跑这个工作流任务。这是 Temporal 防止信号丢失的正常机制；这里在真实的 Temporal 开发服务上确定地造出它
// （拦截器在续接命令提交之前停住，用例这时发信号），证明工作流没有丢信号：
//   - 第一次运行的事件历史里有 cause 为 UNHANDLED_COMMAND 的 WorkflowTaskFailed，之后续接成功；
//   - 这时到的提交在续接前排空、带进下一次运行，被接受且只推一次 TASK_ACCEPTED，排在运行中的写 Task 后面；
//   - 每次运行的历史都能用当前代码回放。设 AR_UPDATE_GOLDEN_HISTORIES=1 时把第一次运行的历史补进 testdata/histories
//     （只补缺的），之后的代码改动也必须能回放带 UNHANDLED_COMMAND 的历史。

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	enumspb "go.temporal.io/api/enums/v1"
	historypb "go.temporal.io/api/history/v1"
	"go.temporal.io/api/temporalproto"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/interceptor"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

const unhandledCommandGolden = "testdata/histories/unhandled_command.json"

// holdContinueAsNew 让第一次决定续接的工作流任务停在提交之前，直到用例放行。
type holdContinueAsNew struct {
	interceptor.WorkerInterceptorBase
	held    atomic.Bool
	reached chan struct{}
	release chan struct{}
}

func (h *holdContinueAsNew) InterceptWorkflow(_ workflow.Context, next interceptor.WorkflowInboundInterceptor) interceptor.WorkflowInboundInterceptor {
	i := &holdInbound{root: h}
	i.Next = next
	return i
}

type holdInbound struct {
	interceptor.WorkflowInboundInterceptorBase
	root *holdContinueAsNew
}

func (i *holdInbound) ExecuteWorkflow(ctx workflow.Context, in *interceptor.ExecuteWorkflowInput) (any, error) {
	ret, err := i.Next.ExecuteWorkflow(ctx, in)
	var can *workflow.ContinueAsNewError
	if errors.As(err, &can) && !workflow.IsReplaying(ctx) && i.root.held.CompareAndSwap(false, true) {
		// 只在测试里：阻塞工作流协程，续接命令要等用例发出信号之后才随这个工作流任务提交。
		close(i.root.reached)
		<-i.root.release
	}
	return ret, err
}

// sessionActivities 代替真实的 AcceptTask、FinishTask，记下调用。
type sessionActivities struct {
	mu       sync.Mutex
	accepted []string
	finished []string
}

func (a *sessionActivities) accept(_ context.Context, _ *commonv1.TenantContext, id string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.accepted = append(a.accepted, id)
	return nil
}

func (a *sessionActivities) finish(_ context.Context, _ *commonv1.TenantContext, r *workflowv1.TaskWorkflowResult) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.finished = append(a.finished, r.GetTaskId())
	return nil
}

func (a *sessionActivities) acceptedIDs() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return slices.Clone(a.accepted)
}

// blockingTask 代替 Python 的 Task 工作流：一直运行，占住写锁。
func blockingTask(ctx workflow.Context, _ *workflowv1.TaskWorkflowInput) (*workflowv1.TaskWorkflowResult, error) {
	return nil, workflow.Sleep(ctx, 24*time.Hour)
}

func startDevServer(t *testing.T) *testsuite.DevServer {
	t.Helper()
	// 用本机装好的 Temporal CLI，不让 SDK 自行下载。
	path, err := exec.LookPath("temporal")
	if err != nil {
		if os.Getenv("AR_ALLOW_MISSING_TOOLS") == "1" {
			t.Skip("找不到 temporal 可执行文件（Temporal CLI）")
		}
		t.Fatal("找不到 temporal 可执行文件（Temporal CLI），见 CLAUDE.md 的常用命令")
	}
	server, err := testsuite.StartDevServer(context.Background(), testsuite.DevServerOptions{ExistingPath: path, LogLevel: "error"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = server.Stop() })
	return server
}

func TestSignalDuringContinueAsNewIsNotLost(t *testing.T) {
	if testing.Short() {
		t.Skip("要拉起 Temporal 开发服务")
	}
	ctx := context.Background()
	// 固定的 identity：golden 历史里不留跑测试的机器名。
	const identity = "session-unhandled-command-test"
	c, err := client.Dial(client.Options{HostPort: startDevServer(t).FrontendHostPort(), Identity: identity})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)

	cfg := DefaultConfig()
	cfg.ContinueAsNewTasks = 1 // 每接受一个 Task 就续接
	hold := &holdContinueAsNew{reached: make(chan struct{}), release: make(chan struct{})}
	var releaseOnce sync.Once
	release := func() { releaseOnce.Do(func() { close(hold.release) }) }
	t.Cleanup(release)

	acts := &sessionActivities{}
	sw := worker.New(c, SessionTaskQueue, worker.Options{
		Identity:                 identity,
		Interceptors:             []interceptor.WorkerInterceptor{hold},
		DeadlockDetectionTimeout: time.Minute, // 拦截器有意阻塞工作流协程
	})
	sw.RegisterWorkflowWithOptions(NewWorkflow(cfg), workflow.RegisterOptions{Name: WorkflowName})
	sw.RegisterActivityWithOptions(acts.accept, activity.RegisterOptions{Name: AcceptTaskActivity})
	sw.RegisterActivityWithOptions(acts.finish, activity.RegisterOptions{Name: FinishTaskActivity})
	tw := worker.New(c, TaskTaskQueue, worker.Options{Identity: identity})
	tw.RegisterWorkflowWithOptions(blockingTask, workflow.RegisterOptions{Name: TaskWorkflowName})
	for _, w := range []worker.Worker{sw, tw} {
		if err := w.Start(); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(w.Stop)
	}

	run, err := c.ExecuteWorkflow(ctx, client.StartWorkflowOptions{ID: testSession, TaskQueue: SessionTaskQueue}, WorkflowName, newInput())
	if err != nil {
		t.Fatal(err)
	}
	first := run.GetRunID()
	// 第一个写 Task：接受、启动之后本次运行的接受数到了上限，工作流决定续接，拦截器停住。
	if err := c.SignalWorkflow(ctx, testSession, first, SignalSubmitTask, submit(1, "c1", write, 0, "")); err != nil {
		t.Fatal(err)
	}
	select {
	case <-hold.reached:
	case <-time.After(time.Minute):
		t.Fatal("一分钟内工作流没有决定续接")
	}
	// 续接命令还没提交：这时到的提交让服务端以 UNHANDLED_COMMAND 拒绝续接。
	if err := c.SignalWorkflow(ctx, testSession, first, SignalSubmitTask, submit(2, "c2", write, 0, "")); err != nil {
		t.Fatal(err)
	}
	release()

	settled := func() (*workflowv1.SessionState, bool) {
		v, err := c.QueryWorkflow(ctx, testSession, "", QuerySessionState)
		if err != nil {
			return nil, false
		}
		st := &workflowv1.SessionState{}
		if err := v.Get(st); err != nil {
			return nil, false
		}
		// 计数先于 TASK_ACCEPTED 的 activity 变化：两样都到了才算稳定。
		return st, st.GetAcceptedTasks() == 2 && st.GetRunningWriteTaskId() == taskID(1) && slices.Equal(st.GetQueuedTaskIds(), []string{taskID(2)}) &&
			len(acts.acceptedIDs()) >= 2
	}
	deadline := time.Now().Add(time.Minute)
	var st *workflowv1.SessionState
	for ok := false; !ok; st, ok = settled() {
		if time.Now().After(deadline) {
			t.Fatalf("一分钟内第二个提交没有被接受并排在写 Task 后面，最后的状态：%v", st)
		}
		time.Sleep(200 * time.Millisecond)
	}
	if got := acts.acceptedIDs(); !slices.Equal(got, []string{taskID(1), taskID(2)}) {
		t.Errorf("每个 Task 只推一次 TASK_ACCEPTED，实际 %v", got)
	}

	runs := sessionRuns(t, c, first)
	if !hasUnhandledCommand(runs[0]) {
		t.Fatal("第一次运行的历史里没有 UNHANDLED_COMMAND：没能造出续接时来信号的情形")
	}
	next := continuedWith(t, runs[0])
	if !slices.ContainsFunc(next.GetPending(), func(p *workflowv1.SubmitTaskSignal) bool { return p.GetTask().GetTaskId() == taskID(2) }) {
		t.Errorf("续接前没有排空这时到的提交：下一次运行的 pending 是 %v", next.GetPending())
	}

	other := DefaultConfig()
	other.ReadOnlyLimit = 97 // 与录制时不同：参数经 SideEffect 记入历史
	r := worker.NewWorkflowReplayer()
	r.RegisterWorkflowWithOptions(NewWorkflow(other), workflow.RegisterOptions{Name: WorkflowName})
	for i, h := range runs {
		if err := r.ReplayWorkflowHistory(nil, h); err != nil {
			t.Errorf("第 %d 次运行的历史回放失败：%v", i+1, err)
		}
	}
	writeGoldenIfMissing(t, runs[0])
}

// sessionRuns 从第一次运行起顺着续接取出每次运行的事件历史，直到仍在运行的那一次。
func sessionRuns(t *testing.T, c client.Client, runID string) []*historypb.History {
	t.Helper()
	var runs []*historypb.History
	for runID != "" {
		h := &historypb.History{}
		it := c.GetWorkflowHistory(context.Background(), testSession, runID, false, enumspb.HISTORY_EVENT_FILTER_TYPE_ALL_EVENT)
		for it.HasNext() {
			ev, err := it.Next()
			if err != nil {
				t.Fatal(err)
			}
			h.Events = append(h.Events, ev)
		}
		runs = append(runs, h)
		runID = ""
		if last := h.Events[len(h.Events)-1]; last.GetEventType() == enumspb.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW {
			runID = last.GetWorkflowExecutionContinuedAsNewEventAttributes().GetNewExecutionRunId()
		}
	}
	return runs
}

func hasUnhandledCommand(h *historypb.History) bool {
	return slices.ContainsFunc(h.Events, func(ev *historypb.HistoryEvent) bool {
		return ev.GetWorkflowTaskFailedEventAttributes().GetCause() == enumspb.WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_COMMAND
	})
}

func continuedWith(t *testing.T, h *historypb.History) *workflowv1.SessionWorkflowInput {
	t.Helper()
	last := h.Events[len(h.Events)-1]
	if last.GetEventType() != enumspb.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW {
		t.Fatalf("第一次运行应以续接结束，实际最后一个事件是 %v", last.GetEventType())
	}
	in := &workflowv1.SessionWorkflowInput{}
	if err := converter.GetDefaultDataConverter().FromPayloads(last.GetWorkflowExecutionContinuedAsNewEventAttributes().GetInput(), in); err != nil {
		t.Fatalf("续接的输入无法解码：%v", err)
	}
	return in
}

func writeGoldenIfMissing(t *testing.T, h *historypb.History) {
	t.Helper()
	if os.Getenv("AR_UPDATE_GOLDEN_HISTORIES") != "1" {
		return
	}
	if _, err := os.Stat(unhandledCommandGolden); err == nil {
		return // 只补缺的：已有的 golden 证明旧版本的历史仍能回放
	}
	data, err := temporalproto.CustomJSONMarshalOptions{Indent: "  "}.Marshal(h)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.FromSlash(unhandledCommandGolden), append(data, '\n'), 0o644); err != nil {
		t.Fatal(err)
	}
}
