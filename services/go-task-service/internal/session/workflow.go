// Package session 是 Session 工作流（设计书 3.1.1、附录 K.1）：接收 Task 信号，写 Task 串行、只读 Task 并行
// 启动 Task 子工作流，提供 session_state 查询，事件历史过长时 Continue-As-New。Task State 不在这里。
//
// Task 子工作流的结束有两条通知：同一次运行里启动的子工作流有 future；Continue-As-New 之后，上一次运行
// 启动的子工作流只能靠它发来的 task_finished 信号。两条都处理，按 task_id 去重，每个 Task 只收尾一次。
package session

import (
	"errors"
	"sort"
	"time"

	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"

	temporaladapter "github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

// NewWorkflow 返回 Session 工作流函数。cfg 只在首次执行时经 SideEffect 记入历史。
func NewWorkflow(cfg Config) func(ctx workflow.Context, in *workflowv1.SessionWorkflowInput) error {
	return func(ctx workflow.Context, in *workflowv1.SessionWorkflowInput) error {
		var recorded Config
		if err := workflow.SideEffect(ctx, func(workflow.Context) any { return cfg }).Get(&recorded); err != nil {
			return err
		}
		if in.GetTenant().GetTenantId() == "" {
			return temporal.NewNonRetryableApplicationError("Session 工作流的输入缺少租户", "missing_tenant", nil)
		}
		s := newSession(ctx, recorded, in)
		if err := workflow.SetQueryHandler(ctx, QuerySessionState, s.state); err != nil {
			return err
		}
		return s.run(in.GetPending())
	}
}

// Register 在 Worker 上登记 Session 工作流（名为 WorkflowName）与它的 activity。
func Register(r worker.Registry, cfg Config, acts *Activities) {
	if err := cfg.Validate(); err != nil {
		panic(err)
	}
	r.RegisterWorkflowWithOptions(NewWorkflow(cfg), workflow.RegisterOptions{Name: WorkflowName})
	r.RegisterActivityWithOptions(acts.AcceptTask, activity.RegisterOptions{Name: AcceptTaskActivity})
	r.RegisterActivityWithOptions(acts.FinishTask, activity.RegisterOptions{Name: FinishTaskActivity})
	r.RegisterActivityWithOptions(acts.PublishTaskEvent, activity.RegisterOptions{Name: PublishTaskEventActivity})
}

// childOptions 是 Task 子工作流的启动参数：ID 为 task_id，投到 task 队列，父工作流关闭或续接时不连带关闭，
// memo 记下所属租户，Task Service 才能用绑定租户的 DurableEngine 查询、取消它。
func childOptions(task *workflowv1.TaskWorkflowInput) (workflow.ChildWorkflowOptions, error) {
	if task.GetTaskId() == "" {
		return workflow.ChildWorkflowOptions{}, errors.New("Task 缺少 task_id")
	}
	memo, err := temporaladapter.ScopeMemo(task.GetTenant())
	if err != nil {
		return workflow.ChildWorkflowOptions{}, err
	}
	return workflow.ChildWorkflowOptions{
		WorkflowID:            task.GetTaskId(),
		TaskQueue:             TaskTaskQueue,
		ParentClosePolicy:     enumspb.PARENT_CLOSE_POLICY_ABANDON,
		WorkflowIDReusePolicy: enumspb.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE,
		Memo:                  memo,
		WaitForCancellation:   true,
	}, nil
}

type session struct {
	ctx    workflow.Context
	cfg    Config
	tenant *commonv1.TenantContext
	sel    workflow.Selector

	// queue 是已接受、等待启动的 Task，按接受顺序：写 Task 等前一个写 Task 结束，只读 Task 等并行名额。
	queue        []*workflowv1.SubmitTaskSignal
	runningWrite string
	runningRO    []string          // 按启动顺序
	submitted    map[string]string // client_request_id → task_id
	active       map[string]bool   // 排队或运行中的 task_id

	accepted        int64
	acceptedThisRun int
	generation      int32
}

func newSession(ctx workflow.Context, cfg Config, in *workflowv1.SessionWorkflowInput) *session {
	s := &session{
		ctx:        ctx,
		cfg:        cfg,
		tenant:     in.GetTenant(),
		submitted:  map[string]string{},
		active:     map[string]bool{},
		accepted:   in.GetAcceptedTasks(),
		generation: in.GetRunGeneration(),
	}
	for _, crid := range sortedKeys(in.GetSubmitted()) {
		s.submitted[crid] = in.GetSubmitted()[crid]
	}
	if id := in.GetRunningWriteTaskId(); id != "" {
		s.runningWrite = id
		s.active[id] = true
	}
	for _, id := range in.GetRunningReadOnlyTaskIds() {
		s.runningRO = append(s.runningRO, id)
		s.active[id] = true
	}
	return s
}

func (s *session) warn(msg string, keyvals ...any) { workflow.GetLogger(s.ctx).Warn(msg, keyvals...) }

func (s *session) run(pending []*workflowv1.SubmitTaskSignal) error {
	ctx := s.ctx
	submitCh := workflow.GetSignalChannel(ctx, SignalSubmitTask)
	cancelCh := workflow.GetSignalChannel(ctx, SignalCancelTask)
	finishedCh := workflow.GetSignalChannel(ctx, SignalTaskFinished)

	s.sel = workflow.NewSelector(ctx)
	s.sel.AddReceive(ctx.Done(), func(workflow.ReceiveChannel, bool) {})
	s.sel.AddReceive(submitCh, func(c workflow.ReceiveChannel, _ bool) {
		sig := &workflowv1.SubmitTaskSignal{}
		c.Receive(ctx, sig)
		s.submit(sig)
	})
	s.sel.AddReceive(cancelCh, func(c workflow.ReceiveChannel, _ bool) {
		sig := &workflowv1.CancelTaskSignal{}
		c.Receive(ctx, sig)
		s.cancel(sig.GetTaskId())
	})
	s.sel.AddReceive(finishedCh, func(c workflow.ReceiveChannel, _ bool) {
		result := &workflowv1.TaskWorkflowResult{}
		c.Receive(ctx, result)
		s.finish(result)
	})

	// 上一次运行带来的 pending：已接受的回到队列，排空的信号在这次运行里接受。
	var fresh []*workflowv1.SubmitTaskSignal
	for _, p := range pending {
		id := p.GetTask().GetTaskId()
		if id != "" && s.submitted[p.GetClientRequestId()] == id {
			if !s.active[id] {
				s.active[id] = true
				s.queue = append(s.queue, p)
			}
			continue
		}
		fresh = append(fresh, p)
	}
	for _, p := range fresh {
		s.submit(p)
	}
	s.schedule()

	for ctx.Err() == nil {
		s.sel.Select(ctx)
		if ctx.Err() != nil {
			break
		}
		s.schedule()
		if s.shouldContinueAsNew() {
			return s.continueAsNew(submitCh, cancelCh, finishedCh)
		}
	}
	return ctx.Err()
}

// submit 接受一个 Task：按 client_request_id 去重，推送 TASK_ACCEPTED，再看能否立即启动。
func (s *session) submit(sig *workflowv1.SubmitTaskSignal) {
	task := sig.GetTask()
	id, crid := task.GetTaskId(), sig.GetClientRequestId()
	if id == "" || crid == "" || task.GetTenant().GetTenantId() != s.tenant.GetTenantId() {
		s.warn("忽略不合法的提交信号", "task_id", id)
		return
	}
	if _, dup := s.submitted[crid]; dup || s.active[id] {
		return
	}
	s.submitted[crid] = id
	s.active[id] = true
	s.accepted++
	s.acceptedThisRun++
	s.queue = append(s.queue, sig)
	s.publishAccepted(id) // replay-mutation:A
	s.schedule()          // replay-mutation:B
}

// VersionAcceptDrainedCancel 是版本分支（S-07 场景 8）：续接前排空信号时，排空的提交随即又被取消。
// 旧版本不接受它就直接推 TASK_FINISHED，它不计入已接受、不进去重表、也没有 TASK_ACCEPTED；
// 新版本先按平时的方式接受，再取消。
const VersionAcceptDrainedCancel = "s07-accept-drained-cancel"

// acceptQueued 接受一个提交并放进队列，但不调度（续接前排空信号时用）。
func (s *session) acceptQueued(sig *workflowv1.SubmitTaskSignal) {
	task := sig.GetTask()
	id, crid := task.GetTaskId(), sig.GetClientRequestId()
	if id == "" || crid == "" || task.GetTenant().GetTenantId() != s.tenant.GetTenantId() {
		s.warn("忽略不合法的提交信号", "task_id", id)
		return
	}
	s.submitted[crid] = id
	s.active[id] = true
	s.accepted++
	s.acceptedThisRun++
	s.queue = append(s.queue, sig)
	s.publishAccepted(id)
}

func isReadOnly(sig *workflowv1.SubmitTaskSignal) bool {
	return sig.GetTask().GetSessionAccess() == commonv1.SessionAccess_SESSION_ACCESS_READ_ONLY
}

// schedule 按接受顺序启动能启动的 Task：写 Task 同一时间只有一个，只读 Task 不超过上限。
func (s *session) schedule() {
	var waiting []*workflowv1.SubmitTaskSignal
	for _, sig := range s.queue {
		switch {
		case isReadOnly(sig) && len(s.runningRO) < s.cfg.ReadOnlyLimit:
			s.start(sig)
		case !isReadOnly(sig) && s.runningWrite == "":
			s.start(sig)
		default:
			waiting = append(waiting, sig)
		}
	}
	s.queue = waiting
}

func (s *session) start(sig *workflowv1.SubmitTaskSignal) {
	task := sig.GetTask()
	id := task.GetTaskId()
	opts, err := childOptions(task)
	if err != nil {
		delete(s.active, id)
		s.publishFinished(failedResult(id, "invalid_task", err))
		return
	}
	childCtx, _ := workflow.NewDisconnectedContext(s.ctx)
	childCtx = workflow.WithChildOptions(childCtx, opts)
	future := workflow.ExecuteChildWorkflow(childCtx, TaskWorkflowName, task)
	var execution workflow.Execution
	if err := future.GetChildWorkflowExecution().Get(s.ctx, &execution); err != nil {
		delete(s.active, id)
		var started *temporal.ChildWorkflowExecutionAlreadyStartedError
		if errors.As(err, &started) {
			// 同一 task_id 已经跑过（去重表淘汰后的重复投递）：不跑第二次，也不重复发结束事件。
			s.warn("Task 已经运行过，忽略重复投递", "task_id", id)
			return
		}
		s.publishFinished(failedResult(id, "start_failed", err))
		return
	}
	if isReadOnly(sig) {
		s.runningRO = append(s.runningRO, id)
	} else {
		s.runningWrite = id
	}
	s.sel.AddFuture(future, func(f workflow.Future) { s.childDone(id, f) })
}

func (s *session) childDone(id string, f workflow.Future) {
	result := &workflowv1.TaskWorkflowResult{}
	if err := f.Get(s.ctx, result); err != nil {
		result = resultFromError(id, err)
	}
	if result.GetTaskId() == "" {
		result.TaskId = id
	}
	s.finish(result)
}

func failedResult(id, code string, err error) *workflowv1.TaskWorkflowResult {
	return &workflowv1.TaskWorkflowResult{
		TaskId: id,
		Status: commonv1.TaskStatus_TASK_STATUS_FAILED,
		Error:  &commonv1.Error{Code: code, Message: err.Error()},
	}
}

func resultFromError(id string, err error) *workflowv1.TaskWorkflowResult {
	switch {
	case temporal.IsCanceledError(err):
		return &workflowv1.TaskWorkflowResult{TaskId: id, Status: commonv1.TaskStatus_TASK_STATUS_CANCELLED}
	case temporal.IsTerminatedError(err):
		return failedResult(id, "terminated", err)
	case temporal.IsTimeoutError(err):
		return failedResult(id, "timed_out", err)
	default:
		return failedResult(id, "task_workflow_failed", err)
	}
}

func (s *session) running(id string) bool {
	if id == "" {
		return false
	}
	if id == s.runningWrite {
		return true
	}
	for _, r := range s.runningRO {
		if r == id {
			return true
		}
	}
	return false
}

// finish 收尾一个运行中的 Task；不在运行中的（未知或已收尾）忽略。
func (s *session) finish(result *workflowv1.TaskWorkflowResult) {
	id := result.GetTaskId()
	if !s.running(id) {
		return
	}
	if id == s.runningWrite {
		s.runningWrite = ""
	} else {
		kept := s.runningRO[:0]
		for _, r := range s.runningRO {
			if r != id {
				kept = append(kept, r)
			}
		}
		s.runningRO = kept
	}
	delete(s.active, id)
	s.publishFinished(result)
}

// cancel 取消一个 Task：排队中的移出队列并直接收尾；运行中的请求取消子工作流，等它收尾后再结束。
func (s *session) cancel(id string) {
	for i, sig := range s.queue {
		if sig.GetTask().GetTaskId() == id {
			s.queue = append(s.queue[:i:i], s.queue[i+1:]...)
			delete(s.active, id)
			s.publishFinished(&workflowv1.TaskWorkflowResult{TaskId: id, Status: commonv1.TaskStatus_TASK_STATUS_CANCELLED})
			return
		}
	}
	if s.running(id) {
		if err := workflow.RequestCancelExternalWorkflow(s.ctx, id, "").Get(s.ctx, nil); err != nil {
			s.warn("取消 Task 子工作流失败", "task_id", id, "error", err)
		}
	}
}

func (s *session) activityCtx() workflow.Context {
	return workflow.WithActivityOptions(s.ctx, workflow.ActivityOptions{
		StartToCloseTimeout: s.cfg.ActivityTimeout,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    time.Second,
			BackoffCoefficient: 2,
			MaximumInterval:    10 * time.Second,
			MaximumAttempts:    5,
		},
	})
}

// 事件推送失败不影响 Task 的调度：记下警告，客户端仍可经查询拿到状态。
func (s *session) publishAccepted(id string) {
	if err := workflow.ExecuteActivity(s.activityCtx(), AcceptTaskActivity, s.tenant, id).Get(s.ctx, nil); err != nil {
		s.warn("推送 TASK_ACCEPTED 失败", "task_id", id, "error", err)
	}
}

func (s *session) publishFinished(result *workflowv1.TaskWorkflowResult) {
	if err := workflow.ExecuteActivity(s.activityCtx(), FinishTaskActivity, s.tenant, result).Get(s.ctx, nil); err != nil {
		s.warn("推送 TASK_FINISHED 失败", "task_id", result.GetTaskId(), "error", err)
	}
}

func (s *session) state() (*workflowv1.SessionState, error) {
	queued := make([]string, 0, len(s.queue))
	for _, sig := range s.queue {
		queued = append(queued, sig.GetTask().GetTaskId())
	}
	return &workflowv1.SessionState{
		SessionId:              s.tenant.GetSessionId(),
		QueuedTaskIds:          queued,
		RunningWriteTaskId:     s.runningWrite,
		RunningReadOnlyTaskIds: append([]string(nil), s.runningRO...),
		AcceptedTasks:          s.accepted,
		RunGeneration:          s.generation,
	}, nil
}

func (s *session) shouldContinueAsNew() bool {
	info := workflow.GetInfo(s.ctx)
	return info.GetContinueAsNewSuggested() ||
		info.GetCurrentHistoryLength() >= s.cfg.ContinueAsNewEvents ||
		(s.cfg.ContinueAsNewTasks > 0 && s.acceptedThisRun >= s.cfg.ContinueAsNewTasks)
}

// continueAsNew 排空未处理的信号与已就绪的子工作流结果，把状态带入新的运行。
// 排空的提交不在这次运行里接受（去掉重复），放进 pending 由新的运行接受。
func (s *session) continueAsNew(submitCh, cancelCh, finishedCh workflow.ReceiveChannel) error {
	var fresh []*workflowv1.SubmitTaskSignal
	seen := map[string]bool{}
	for {
		if err := s.ctx.Err(); err != nil {
			return err
		}
		drained := false
		for {
			sig := &workflowv1.SubmitTaskSignal{}
			if !submitCh.ReceiveAsync(sig) {
				break
			}
			drained = true
			crid := sig.GetClientRequestId()
			if _, dup := s.submitted[crid]; dup || seen[crid] || crid == "" {
				continue
			}
			seen[crid] = true
			fresh = append(fresh, sig)
		}
		for {
			result := &workflowv1.TaskWorkflowResult{}
			if !finishedCh.ReceiveAsync(result) {
				break
			}
			drained = true
			s.finish(result)
		}
		for {
			sig := &workflowv1.CancelTaskSignal{}
			if !cancelCh.ReceiveAsync(sig) {
				break
			}
			drained = true
			if i := indexOf(fresh, sig.GetTaskId()); i >= 0 {
				pending := fresh[i]
				fresh = append(fresh[:i:i], fresh[i+1:]...)
				if workflow.GetVersion(s.ctx, VersionAcceptDrainedCancel, workflow.DefaultVersion, 1) == workflow.DefaultVersion {
					s.publishFinished(&workflowv1.TaskWorkflowResult{TaskId: sig.GetTaskId(), Status: commonv1.TaskStatus_TASK_STATUS_CANCELLED})
					continue
				}
				// 与平时的顺序一致：先接受（计数、去重表、TASK_ACCEPTED），再从队列里取消。
				s.acceptQueued(pending)
				s.cancel(sig.GetTaskId())
				continue
			}
			s.cancel(sig.GetTaskId())
		}
		// 信号通道已空，此时就绪的只会是子工作流的结果。
		if s.sel.HasPending() {
			s.sel.Select(s.ctx)
			drained = true
		}
		if !drained {
			break
		}
	}
	next := &workflowv1.SessionWorkflowInput{
		Tenant:                 s.tenant,
		Pending:                append(append([]*workflowv1.SubmitTaskSignal(nil), s.queue...), fresh...),
		RunningWriteTaskId:     s.runningWrite,
		RunningReadOnlyTaskIds: append([]string(nil), s.runningRO...),
		Submitted:              s.boundedSubmitted(),
		AcceptedTasks:          s.accepted,
		RunGeneration:          s.generation + 1,
	}
	return workflow.NewContinueAsNewError(s.ctx, WorkflowName, next)
}

func indexOf(sigs []*workflowv1.SubmitTaskSignal, id string) int {
	for i, sig := range sigs {
		if sig.GetTask().GetTaskId() == id {
			return i
		}
	}
	return -1
}

// boundedSubmitted 返回带入新运行的去重表：未结束的 Task 全部保留，已结束的按 task_id（ULID，越大越新）
// 保留最新的，总数不超过 DedupeLimit。
func (s *session) boundedSubmitted() map[string]string {
	out := map[string]string{}
	type entry struct{ crid, id string }
	var done []entry
	for _, crid := range sortedKeys(s.submitted) {
		id := s.submitted[crid]
		if s.active[id] {
			out[crid] = id
		} else {
			done = append(done, entry{crid, id})
		}
	}
	sort.SliceStable(done, func(i, j int) bool { return done[i].id > done[j].id })
	for _, e := range done {
		if len(out) >= s.cfg.DedupeLimit {
			break
		}
		out[e.crid] = e.id
	}
	return out
}

// sortedKeys 取出 map 的键并排序，遍历顺序因此确定。
func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
