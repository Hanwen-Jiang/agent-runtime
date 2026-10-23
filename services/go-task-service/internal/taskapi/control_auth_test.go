package taskapi

// R-05 评审 D9（docs/plan/review-R-05-ingress-sse.md 第 4 节）：人工回复（包括 write_irreversible 的审批）与取消，
// 只接受 Task 的提交者，或身份带 task.approve scope 的用户；同租户的其他人 PermissionDenied，请求不触达 DurableEngine。
// 查询仍对同租户开放。没记下提交者的旧 Task 只接受带 scope 的用户。

import (
	"context"
	"testing"
	"time"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

func (f *apiFixture) reply(token, taskID string) error {
	_, err := f.client.SignalTask(context.Background(), authed(token, &taskapiv1.SignalTaskRequest{
		TaskId: taskID,
		Signal: &taskapiv1.SignalTaskRequest_HumanReply{HumanReply: &taskapiv1.HumanReply{QuestionId: "pay.1", Option: "approve"}},
	}))
	return err
}

func (f *apiFixture) cancel(token, taskID string) error {
	_, err := f.client.CancelTask(context.Background(), authed(token, &taskapiv1.CancelTaskRequest{TaskId: taskID}))
	return err
}

func TestOnlyTheSubmitterOrAnApproverMayReplyOrCancel(t *testing.T) {
	f := newAPI(t)
	owner := f.token("t1", "u1")
	sub := f.mustSubmit(owner, newRequest("c1"))
	f.durable.addWorkflow(sub.GetTaskId(), "t1", session.TaskWorkflowName, durable.StatusRunning, nil)

	colleague := f.token("t1", "u2")
	wantCode(t, "同租户的别人发人工回复（审批）", f.reply(colleague, sub.GetTaskId()), connect.CodePermissionDenied)
	wantCode(t, "同租户的别人取消", f.cancel(colleague, sub.GetTaskId()), connect.CodePermissionDenied)
	if n := len(f.durable.callsOf("Signal")); n != 0 {
		t.Fatalf("被拒的控制指令不能触达 DurableEngine，实际发出 %d 个信号", n)
	}
	if _, err := f.client.QueryTask(context.Background(), authed(colleague, &taskapiv1.QueryTaskRequest{TaskId: sub.GetTaskId()})); err != nil {
		t.Errorf("查询仍对同租户开放：%v", err)
	}

	approver := f.ingress.IssueTokenWithScopes("t1", "u3", time.Hour, ScopeApprove)
	if err := f.reply(approver, sub.GetTaskId()); err != nil {
		t.Fatalf("带 %s 的用户可以审批：%v", ScopeApprove, err)
	}
	calls := f.durable.callsOf("Signal")
	if got := calls[len(calls)-1].Payload.(*workflowv1.HumanReplySignal).GetRepliedByUserId(); got != "u3" {
		t.Errorf("审批人记为实际回复的人，实际 %q", got)
	}
	if err := f.reply(owner, sub.GetTaskId()); err != nil {
		t.Errorf("提交者可以回复：%v", err)
	}
	if err := f.cancel(approver, sub.GetTaskId()); err != nil {
		t.Errorf("带 %s 的用户可以取消：%v", ScopeApprove, err)
	}
	if err := f.cancel(owner, sub.GetTaskId()); err != nil {
		t.Errorf("提交者可以取消：%v", err)
	}
}

func TestTheSubmitterIsRecorded(t *testing.T) {
	f := newAPI(t)
	sub := f.mustSubmit(f.token("t1", "u1"), newRequest("c1"))
	row, ok, err := f.tasks.LookupTask(context.Background(), &commonv1.TenantContext{TenantId: "t1"}, sub.GetTaskId())
	if err != nil || !ok {
		t.Fatalf("业务库里应有这个 Task：%v", err)
	}
	if row.SubmittedBy != "u1" {
		t.Errorf("记下提交者，实际 %q", row.SubmittedBy)
	}
}

func TestTasksWithoutARecordedSubmitterNeedAnApprover(t *testing.T) {
	f := newAPI(t)
	tenant := &commonv1.TenantContext{TenantId: "t1", UserId: "u1"}
	sessionID, taskID := "ses_01J"+"0000000000000000000000"+"V", "tsk_01J"+"0000000000000000000000"+"V"
	if err := f.tasks.CreateSession(context.Background(), tenant, sessionID, f.clock.Now()); err != nil {
		t.Fatal(err)
	}
	// 迁移之前提交的 Task：没有提交者。
	if _, _, err := f.tasks.InsertTask(context.Background(), tenant, taskstore.Task{
		TaskID: taskID, SessionID: sessionID, ClientRequestID: "legacy", CreatedAt: f.clock.Now(),
	}); err != nil {
		t.Fatal(err)
	}
	f.durable.addWorkflow(taskID, "t1", session.TaskWorkflowName, durable.StatusRunning, nil)
	wantCode(t, "没记下提交者时，普通用户不能审批", f.reply(f.token("t1", "u1"), taskID), connect.CodePermissionDenied)
	if err := f.reply(f.ingress.IssueTokenWithScopes("t1", "u3", time.Hour, ScopeApprove), taskID); err != nil {
		t.Errorf("带 %s 的用户照样可以审批：%v", ScopeApprove, err)
	}
}
