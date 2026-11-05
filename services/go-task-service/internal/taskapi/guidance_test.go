package taskapi

// docs/plan/running-task-interaction.md 第 2 节验收：SignalTask 接受纠偏、修改、撤回，以及修改排队中的 Task 的消息。
//
// 权限同取消：提交者本人，或带 task.approve 的用户；租户取自认证身份。纠偏发给 Task 工作流（谁的信号先到谁算，结果靠事件
// guidance_updated 告诉客户端）；修改排队中的消息发给 Session 工作流（队列在它那里）。

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/genproto/googleapis/rpc/errdetails"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
)

const (
	nextStep  = commonv1.GuidanceMode_GUIDANCE_MODE_NEXT_STEP
	interrupt = commonv1.GuidanceMode_GUIDANCE_MODE_INTERRUPT
)

func submitGuidance(id, text string, mode commonv1.GuidanceMode) *taskapiv1.SignalTaskRequest_SubmitGuidance {
	return &taskapiv1.SignalTaskRequest_SubmitGuidance{SubmitGuidance: &taskapiv1.SubmitGuidance{GuidanceId: id, Text: text, Mode: mode}}
}

func editGuidance(id, text string) *taskapiv1.SignalTaskRequest_EditGuidance {
	return &taskapiv1.SignalTaskRequest_EditGuidance{EditGuidance: &taskapiv1.EditGuidance{GuidanceId: id, Text: text}}
}

func retractGuidance(id string) *taskapiv1.SignalTaskRequest_RetractGuidance {
	return &taskapiv1.SignalTaskRequest_RetractGuidance{RetractGuidance: &taskapiv1.RetractGuidance{GuidanceId: id}}
}

func editQueued(message string) *taskapiv1.SignalTaskRequest_EditQueuedMessage {
	return &taskapiv1.SignalTaskRequest_EditQueuedMessage{EditQueuedMessage: &taskapiv1.EditQueuedMessage{Message: message}}
}

// signalOf 发一个 SignalTask。signal 是 SignalTaskRequest 的 oneof。
func (f *apiFixture) signalOf(token, taskID string, set func(*taskapiv1.SignalTaskRequest)) error {
	req := &taskapiv1.SignalTaskRequest{TaskId: taskID}
	set(req)
	_, err := f.client.SignalTask(context.Background(), authed(token, req))
	return err
}

func (f *apiFixture) steer(token, taskID, guidanceID, text string, mode commonv1.GuidanceMode) error {
	return f.signalOf(token, taskID, func(r *taskapiv1.SignalTaskRequest) { r.Signal = submitGuidance(guidanceID, text, mode) })
}

// running 提交一个 Task，并放进一个运行中的子工作流。
func (f *apiFixture) running(token string) string {
	f.t.Helper()
	sub := f.mustSubmit(token, newRequest("c1"))
	f.durable.addWorkflow(sub.GetTaskId(), "t1", session.TaskWorkflowName, durable.StatusRunning, nil)
	return sub.GetTaskId()
}

func (f *apiFixture) lastGuidance(t *testing.T) (durableCall, *workflowv1.TaskGuidanceSignal) {
	t.Helper()
	calls := f.durable.callsOf("Signal")
	if len(calls) == 0 {
		t.Fatal("没有发出信号")
	}
	last := calls[len(calls)-1]
	sig, ok := last.Payload.(*workflowv1.TaskGuidanceSignal)
	if !ok {
		t.Fatalf("信号载荷应是 TaskGuidanceSignal，实际 %T", last.Payload)
	}
	return last, sig
}

// reasonOf 取错误里 google.rpc.ErrorInfo 的 reason（应用按它翻译）。
func reasonOf(err error) string {
	var ce *connect.Error
	if !errors.As(err, &ce) {
		return ""
	}
	for _, d := range ce.Details() {
		if v, e := d.Value(); e == nil {
			if info, ok := v.(*errdetails.ErrorInfo); ok && info.GetDomain() == ErrorDomain {
				return info.GetReason()
			}
		}
	}
	return ""
}

func TestSubmitGuidanceSignalsTheTaskWorkflow(t *testing.T) {
	f := newAPI(t)
	owner := f.token("t1", "u1")
	taskID := f.running(owner)
	f.clock.Advance(time.Minute)
	if err := f.steer(owner, taskID, "g1", "不要改公共接口", interrupt); err != nil {
		t.Fatalf("提交纠偏：%v", err)
	}
	call, sig := f.lastGuidance(t)
	if call.WorkflowID != taskID || call.Name != session.SignalTaskGuidance {
		t.Errorf("要发给 Task 工作流的 %s，实际 %s 的 %s", session.SignalTaskGuidance, call.WorkflowID, call.Name)
	}
	if call.Scope != "t1" {
		t.Errorf("租户作用域取自认证身份，实际 %q", call.Scope)
	}
	if sig.GetGuidanceId() != "g1" || sig.GetText() != "不要改公共接口" || sig.GetMode() != interrupt {
		t.Errorf("载荷不对：%v", sig)
	}
	if sig.GetKind() != workflowv1.TaskGuidanceKind_TASK_GUIDANCE_KIND_SUBMIT {
		t.Errorf("kind 应是 SUBMIT，实际 %v", sig.GetKind())
	}
	if sig.GetByUserId() != "u1" {
		t.Errorf("发送人取自认证身份，实际 %q", sig.GetByUserId())
	}
	if !sig.GetAt().AsTime().Equal(f.clock.Now()) {
		t.Errorf("时间取服务端的，实际 %v", sig.GetAt().AsTime())
	}
}

func TestEditAndRetractSignalTheTaskWorkflow(t *testing.T) {
	f := newAPI(t)
	owner := f.token("t1", "u1")
	taskID := f.running(owner)
	if err := f.signalOf(owner, taskID, func(r *taskapiv1.SignalTaskRequest) { r.Signal = editGuidance("g1", "改过的") }); err != nil {
		t.Fatalf("修改：%v", err)
	}
	_, sig := f.lastGuidance(t)
	if sig.GetKind() != workflowv1.TaskGuidanceKind_TASK_GUIDANCE_KIND_EDIT || sig.GetGuidanceId() != "g1" || sig.GetText() != "改过的" {
		t.Errorf("修改的载荷不对：%v", sig)
	}
	if err := f.signalOf(owner, taskID, func(r *taskapiv1.SignalTaskRequest) { r.Signal = retractGuidance("g1") }); err != nil {
		t.Fatalf("撤回：%v", err)
	}
	_, sig = f.lastGuidance(t)
	if sig.GetKind() != workflowv1.TaskGuidanceKind_TASK_GUIDANCE_KIND_RETRACT || sig.GetGuidanceId() != "g1" || sig.GetText() != "" {
		t.Errorf("撤回的载荷不对：%v", sig)
	}
}

func TestOnlyTheSubmitterOrAnApproverMaySteer(t *testing.T) {
	f := newAPI(t)
	owner := f.token("t1", "u1")
	taskID := f.running(owner)
	colleague := f.token("t1", "u2")
	wantCode(t, "同租户的别人纠偏", f.steer(colleague, taskID, "g1", "x", nextStep), connect.CodePermissionDenied)
	wantCode(t, "同租户的别人撤回", f.signalOf(colleague, taskID, func(r *taskapiv1.SignalTaskRequest) { r.Signal = retractGuidance("g1") }), connect.CodePermissionDenied)
	wantCode(t, "同租户的别人修改", f.signalOf(colleague, taskID, func(r *taskapiv1.SignalTaskRequest) { r.Signal = editGuidance("g1", "x") }), connect.CodePermissionDenied)
	wantCode(t, "同租户的别人改排队的消息", f.signalOf(colleague, taskID, func(r *taskapiv1.SignalTaskRequest) { r.Signal = editQueued("x") }), connect.CodePermissionDenied)
	if n := len(f.durable.callsOf("Signal")); n != 0 {
		t.Fatalf("被拒的请求不能触达 DurableEngine，实际发出 %d 个信号", n)
	}
	approver := f.ingress.IssueTokenWithScopes("t1", "u3", time.Hour, ScopeApprove)
	if err := f.steer(approver, taskID, "g1", "x", nextStep); err != nil {
		t.Fatalf("带 %s 的用户可以纠偏：%v", ScopeApprove, err)
	}
	if _, sig := f.lastGuidance(t); sig.GetByUserId() != "u3" {
		t.Errorf("发送人记为实际发的人，实际 %q", sig.GetByUserId())
	}
}

func TestAnotherTenantCannotSeeTheTask(t *testing.T) {
	f := newAPI(t)
	taskID := f.running(f.token("t1", "u1"))
	wantCode(t, "别的租户纠偏", f.steer(f.token("t2", "u1"), taskID, "g1", "x", nextStep), connect.CodeNotFound)
	if n := len(f.durable.callsOf("Signal")); n != 0 {
		t.Fatalf("别的租户的请求不能触达 DurableEngine，实际发出 %d 个信号", n)
	}
}

func TestInvalidGuidanceIsRejected(t *testing.T) {
	long := strings.Repeat("长", maxMessageBytes/3+1)
	cases := []struct {
		name   string
		signal func(*taskapiv1.SignalTaskRequest)
	}{
		{"没有 id", func(r *taskapiv1.SignalTaskRequest) { r.Signal = submitGuidance("", "x", nextStep) }},
		{"id 里有非法字符", func(r *taskapiv1.SignalTaskRequest) { r.Signal = submitGuidance("g 1", "x", nextStep) }},
		{"id 太长", func(r *taskapiv1.SignalTaskRequest) { r.Signal = submitGuidance(strings.Repeat("g", 65), "x", nextStep) }},
		{"没有文字", func(r *taskapiv1.SignalTaskRequest) { r.Signal = submitGuidance("g1", "", nextStep) }},
		{"只有空白", func(r *taskapiv1.SignalTaskRequest) { r.Signal = submitGuidance("g1", " \n\t", nextStep) }},
		{"文字太长", func(r *taskapiv1.SignalTaskRequest) { r.Signal = submitGuidance("g1", long, nextStep) }},
		{"没有选生效时机", func(r *taskapiv1.SignalTaskRequest) { r.Signal = submitGuidance("g1", "x", commonv1.GuidanceMode_GUIDANCE_MODE_UNSPECIFIED) }},
		{"修改没有文字", func(r *taskapiv1.SignalTaskRequest) { r.Signal = editGuidance("g1", "") }},
		{"修改文字太长", func(r *taskapiv1.SignalTaskRequest) { r.Signal = editGuidance("g1", long) }},
		{"撤回没有 id", func(r *taskapiv1.SignalTaskRequest) { r.Signal = retractGuidance("") }},
		{"排队的消息是空的", func(r *taskapiv1.SignalTaskRequest) { r.Signal = editQueued(" ") }},
		{"排队的消息太长", func(r *taskapiv1.SignalTaskRequest) { r.Signal = editQueued(long) }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := newAPI(t)
			owner := f.token("t1", "u1")
			taskID := f.running(owner)
			wantCode(t, c.name, f.signalOf(owner, taskID, c.signal), connect.CodeInvalidArgument)
			if n := len(f.durable.callsOf("Signal")); n != 0 {
				t.Fatalf("不合法的请求不能发出信号，实际 %d 个", n)
			}
		})
	}
}

// finished 把 Task 记为已结束。
func (f *apiFixture) finished(t *testing.T, taskID string) {
	t.Helper()
	row, ok, err := f.tasks.LookupTask(context.Background(), &commonv1.TenantContext{TenantId: "t1"}, taskID)
	if err != nil || !ok {
		t.Fatalf("查不到 Task：%v", err)
	}
	tenant := &commonv1.TenantContext{TenantId: "t1", SessionId: row.SessionID}
	if err := f.tasks.RecordFinished(context.Background(), tenant, taskID, commonv1.TaskStatus_TASK_STATUS_DONE, 1, `{}`); err != nil {
		t.Fatalf("记结局：%v", err)
	}
}

func TestGuidanceToAFinishedTaskIsRejected(t *testing.T) {
	f := newAPI(t)
	owner := f.token("t1", "u1")
	taskID := f.running(owner)
	f.finished(t, taskID)
	err := f.steer(owner, taskID, "g1", "来晚了", nextStep)
	wantCode(t, "Task 已结束", err, connect.CodeFailedPrecondition)
	if got := reasonOf(err); got != "task_finished" {
		t.Errorf("客户端靠 ErrorInfo 的 task_finished 提示可以作为新任务发送，实际 %q（%v）", got, err)
	}
	if n := len(f.durable.callsOf("Signal")); n != 0 {
		t.Fatalf("已结束的 Task 不发信号，实际 %d 个", n)
	}
}

func TestGuidanceToATaskStillInTheQueueIsRejected(t *testing.T) {
	f := newAPI(t)
	owner := f.token("t1", "u1")
	sub := f.mustSubmit(owner, newRequest("c1"))
	err := f.steer(owner, sub.GetTaskId(), "g1", "还没开始", nextStep)
	wantCode(t, "Task 还在排队", err, connect.CodeFailedPrecondition)
	if got := reasonOf(err); got != "task_not_started" {
		t.Errorf("客户端靠 ErrorInfo 的 task_not_started 提示改排队的消息，实际 %q（%v）", got, err)
	}
}

func TestEditQueuedMessageSignalsTheSessionWorkflow(t *testing.T) {
	f := newAPI(t)
	owner := f.token("t1", "u1")
	sub := f.mustSubmit(owner, newRequest("c1"))
	f.clock.Advance(time.Minute)
	if err := f.signalOf(owner, sub.GetTaskId(), func(r *taskapiv1.SignalTaskRequest) { r.Signal = editQueued("改过的消息") }); err != nil {
		t.Fatalf("修改排队的消息：%v", err)
	}
	calls := f.durable.callsOf("Signal")
	if len(calls) != 1 {
		t.Fatalf("应发一个信号，实际 %d 个", len(calls))
	}
	if calls[0].WorkflowID != sub.GetSessionId() || calls[0].Name != session.SignalEditQueuedTask {
		t.Errorf("队列在 Session 工作流：应发给 %s 的 %s，实际 %s 的 %s", sub.GetSessionId(), session.SignalEditQueuedTask, calls[0].WorkflowID, calls[0].Name)
	}
	sig, ok := calls[0].Payload.(*workflowv1.EditQueuedTaskSignal)
	if !ok {
		t.Fatalf("载荷应是 EditQueuedTaskSignal，实际 %T", calls[0].Payload)
	}
	if sig.GetTaskId() != sub.GetTaskId() || sig.GetMessage() != "改过的消息" || sig.GetByUserId() != "u1" {
		t.Errorf("载荷不对：%v", sig)
	}
	if !sig.GetAt().AsTime().Equal(f.clock.Now()) {
		t.Errorf("时间取服务端的，实际 %v", sig.GetAt().AsTime())
	}
	row, ok, err := f.tasks.LookupTask(context.Background(), &commonv1.TenantContext{TenantId: "t1"}, sub.GetTaskId())
	if err != nil || !ok {
		t.Fatalf("查不到 Task：%v", err)
	}
	if row.Message == "改过的消息" {
		t.Error("改不改由 Session 工作流定（Task 可能刚好开始了）：API 这里不写业务库")
	}
}

func TestEditQueuedMessageOfAFinishedTaskIsRejected(t *testing.T) {
	f := newAPI(t)
	owner := f.token("t1", "u1")
	sub := f.mustSubmit(owner, newRequest("c1"))
	f.finished(t, sub.GetTaskId())
	err := f.signalOf(owner, sub.GetTaskId(), func(r *taskapiv1.SignalTaskRequest) { r.Signal = editQueued("来晚了") })
	wantCode(t, "Task 已结束", err, connect.CodeFailedPrecondition)
	if n := len(f.durable.callsOf("Signal")); n != 0 {
		t.Fatalf("已结束的 Task 不发信号，实际 %d 个", n)
	}
}

func TestHumanReplyStillWorks(t *testing.T) {
	f := newAPI(t)
	owner := f.token("t1", "u1")
	taskID := f.running(owner)
	if err := f.reply(owner, taskID); err != nil {
		t.Fatalf("人工回复：%v", err)
	}
	calls := f.durable.callsOf("Signal")
	if len(calls) != 1 || calls[0].Name != session.SignalHumanReply {
		t.Fatalf("人工回复的信号不变：%+v", calls)
	}
}
