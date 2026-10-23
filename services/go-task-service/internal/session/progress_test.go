package session

// T-11b 验收（R-05 评审 D10；设计书 4.14、附录 K.1）：Python 的 Task 工作流经跨语言 activity publish_task_event，
// 把进度事件交给 Session 的 Worker。这里验 Go 一侧：
//   - event_id 由业务库分配，接在 task_accepted 之后、task_finished 之前，严格递增；
//   - 同一个去重键只推一次（activity 重试），返回同一个 event_id；
//   - Task 已经结束（业务库记下了结局）之后到的事件丢弃，返回 0，task_finished 仍是最后一个；
//   - task_accepted、task_finished、heartbeat 与未指定的类型只能由 Session 自己推：不可重试的错误；
//   - 别的租户的 Task、缺少租户：不可重试的错误，什么也不推；
//   - Register 按契约里的名字登记这个 activity。

import (
	"context"
	"errors"
	"testing"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

// emittedAt 是 Task 工作流写进事件的时间（workflow.now()），Session 的 Worker 原样保留。
var emittedAt = time.Date(2026, 10, 23, 8, 0, 42, 0, time.UTC)

func progress(t *testing.T, id string, typ taskapiv1.TaskEventType, payload map[string]any) *taskapiv1.TaskEvent {
	t.Helper()
	v, err := structpb.NewValue(payload)
	if err != nil {
		t.Fatal(err)
	}
	return &taskapiv1.TaskEvent{
		TaskId: id, Type: typ, EmittedAt: timestamppb.New(emittedAt), Payload: &commonv1.JsonPayload{Value: v},
	}
}

func roundStarted(t *testing.T, id string, seq int) *taskapiv1.TaskEvent {
	return progress(t, id, taskapiv1.TaskEventType_TASK_EVENT_TYPE_ROUND_STARTED, map[string]any{"event": "round_started", "round_seq": seq, "rerun": false})
}

func nonRetryable(t *testing.T, what string, err error) {
	t.Helper()
	var app *temporal.ApplicationError
	if !errors.As(err, &app) || !app.NonRetryable() {
		t.Errorf("%s：应返回不可重试的错误，实际 %v", what, err)
	}
}

func TestPublishTaskEventFollowsTaskAccepted(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(11)
	f.insertTask(t, testTenant(), id)
	if err := f.acts.AcceptTask(ctx, testTenant(), id); err != nil {
		t.Fatal(err)
	}

	first, err := f.acts.PublishTaskEvent(ctx, testTenant(), "r1/round_started", roundStarted(t, id, 1))
	if err != nil {
		t.Fatal(err)
	}
	verdict := progress(t, id, taskapiv1.TaskEventType_TASK_EVENT_TYPE_VERDICT, map[string]any{"event": "verdict", "round_seq": 1, "verdict": "done", "reason": "结果齐全"})
	second, err := f.acts.PublishTaskEvent(ctx, testTenant(), "r1/verdict", verdict)
	if err != nil {
		t.Fatal(err)
	}
	if first != 2 || second != 3 {
		t.Fatalf("event_id 接着 task_accepted（1）往后排：应为 2、3，实际 %d、%d", first, second)
	}
	if err := f.acts.FinishTask(ctx, testTenant(), &workflowv1.TaskWorkflowResult{TaskId: id, Status: commonv1.TaskStatus_TASK_STATUS_DONE}); err != nil {
		t.Fatal(err)
	}

	evs := f.events(t, testTenant(), id)
	wantTypes := []taskapiv1.TaskEventType{
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED,
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_ROUND_STARTED,
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_VERDICT,
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED,
	}
	if len(evs) != len(wantTypes) {
		t.Fatalf("流里应依次是 task_accepted、round_started、verdict、task_finished，实际 %v", evs)
	}
	for i, ev := range evs {
		if ev.GetType() != wantTypes[i] || ev.GetEventId() != int64(i+1) {
			t.Errorf("第 %d 个事件应为 %v（event_id %d），实际 %v", i+1, wantTypes[i], i+1, ev)
		}
	}
	got := evs[1]
	if got.GetSessionId() != testSession || got.GetTaskId() != id || !got.GetEmittedAt().AsTime().Equal(emittedAt) {
		t.Errorf("session_id 取自租户上下文，emitted_at 用事件里的（工作流时间），实际 %v", got)
	}
	if fields := got.GetPayload().GetValue().GetStructValue().GetFields(); fields["event"].GetStringValue() != "round_started" || fields["round_seq"].GetNumberValue() != 1 {
		t.Errorf("载荷原样保留，实际 %v", got.GetPayload())
	}
}

func TestPublishTaskEventIsIdempotentPerKey(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(12)
	f.insertTask(t, testTenant(), id)
	ids := make([]int64, 0, 3)
	for range 2 { // activity 重试：同一个去重键
		n, err := f.acts.PublishTaskEvent(ctx, testTenant(), "r1/round_started", roundStarted(t, id, 1))
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, n)
	}
	n, err := f.acts.PublishTaskEvent(ctx, testTenant(), "r2/round_started", roundStarted(t, id, 2))
	if err != nil {
		t.Fatal(err)
	}
	ids = append(ids, n)
	if ids[0] != 1 || ids[1] != 1 || ids[2] != 2 {
		t.Errorf("同一个去重键只推一次、返回同一个 event_id，换了键才推新的：实际 %v", ids)
	}
	if evs := f.events(t, testTenant(), id); len(evs) != 2 {
		t.Errorf("流里应只有两个事件，实际 %d 个", len(evs))
	}
}

func TestEventsArrivingAfterTheTaskFinishedAreDropped(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(13)
	f.insertTask(t, testTenant(), id)
	if err := f.acts.FinishTask(ctx, testTenant(), &workflowv1.TaskWorkflowResult{TaskId: id, Status: commonv1.TaskStatus_TASK_STATUS_DONE}); err != nil {
		t.Fatal(err)
	}
	n, err := f.acts.PublishTaskEvent(ctx, testTenant(), "r1/verdict", progress(t, id, taskapiv1.TaskEventType_TASK_EVENT_TYPE_VERDICT,
		map[string]any{"event": "verdict", "round_seq": 1, "verdict": "done", "reason": "迟到"}))
	if err != nil || n != 0 {
		t.Fatalf("Task 结束之后到的事件丢弃、不报错，返回 0；实际 %d（%v）", n, err)
	}
	evs := f.events(t, testTenant(), id)
	if len(evs) != 1 || evs[0].GetType() != taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED {
		t.Errorf("task_finished 仍是最后一个事件，实际 %v", evs)
	}
	if row, _, _ := f.tasks.LookupTask(ctx, testTenant(), id); row.LastEventID != 1 {
		t.Errorf("丢弃的事件不占 event_id，实际最后的 event_id %d", row.LastEventID)
	}
}

func TestReservedEventTypesCannotBePublishedByTheTask(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(14)
	f.insertTask(t, testTenant(), id)
	for _, typ := range []taskapiv1.TaskEventType{
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_UNSPECIFIED,
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED,
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED,
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_HEARTBEAT,
	} {
		_, err := f.acts.PublishTaskEvent(ctx, testTenant(), "k-"+typ.String(), progress(t, id, typ, map[string]any{"event": "x"}))
		nonRetryable(t, typ.String(), err)
	}
	_, err := f.acts.PublishTaskEvent(ctx, testTenant(), "no-task", roundStarted(t, "", 1))
	nonRetryable(t, "没有 task_id", err)
	_, err = f.acts.PublishTaskEvent(ctx, testTenant(), "", roundStarted(t, id, 1))
	nonRetryable(t, "没有去重键", err)
	if evs := f.events(t, testTenant(), id); len(evs) != 0 {
		t.Errorf("被拒的事件一个也不推，实际 %v", evs)
	}
}

func TestPublishingForAnotherTenantsTaskIsRejected(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(15)
	f.insertTask(t, testTenant(), id)
	other := &commonv1.TenantContext{TenantId: "t2", UserId: "u9", SessionId: testSession}
	_, err := f.acts.PublishTaskEvent(ctx, other, "r1/round_started", roundStarted(t, id, 1))
	nonRetryable(t, "别的租户的 Task", err)
	_, err = f.acts.PublishTaskEvent(ctx, nil, "r1/round_started", roundStarted(t, id, 1))
	nonRetryable(t, "缺少租户", err)
	if evs := f.events(t, testTenant(), id); len(evs) != 0 {
		t.Errorf("t1 的流里不该多出事件，实际 %v", evs)
	}
	if evs := f.events(t, other, id); len(evs) != 0 {
		t.Errorf("t2 名下也不该有这个 Task 的事件，实际 %v", evs)
	}
}

// recordingRegistry 只记下 Register 登记了哪些 activity；Register 用不到的方法留给嵌入的接口（调用即 panic）。
type recordingRegistry struct {
	worker.Registry
	activities []string
}

func (r *recordingRegistry) RegisterWorkflowWithOptions(any, workflow.RegisterOptions) {}

func (r *recordingRegistry) RegisterActivityWithOptions(_ any, o activity.RegisterOptions) {
	r.activities = append(r.activities, o.Name)
}

func TestPublishTaskEventIsRegisteredUnderTheContractName(t *testing.T) {
	r := &recordingRegistry{}
	Register(r, DefaultConfig(), &Activities{})
	for _, name := range r.activities {
		if name == PublishTaskEventActivity {
			return
		}
	}
	t.Errorf("Session 的 Worker 应登记 %s（Task 工作流按这个名字调用），实际只登记了 %v", PublishTaskEventActivity, r.activities)
}
