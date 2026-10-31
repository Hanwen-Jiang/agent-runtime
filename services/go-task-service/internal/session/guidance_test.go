package session

// docs/plan/running-task-interaction.md 第 2 节验收：修改排队中的 Task 的消息。
//
// 队列在 Session 工作流里，工作流是单线程的：Task 开始之前到的信号算数，原位替换、保持顺序；开始之后到的不改。结果经活动
// RecordQueuedEdit 记进业务库并推 task_message_edited（不带文字，客户端重新查 Task）。

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/mock"
	"google.golang.org/protobuf/types/known/timestamppb"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
)

type queuedEdit struct {
	taskID  string
	message string
	applied bool
	reason  string
}

// recordEdits 记下 Session 工作流交给 RecordQueuedEdit 的结果。
func recordEdits(h *harness) *[]queuedEdit {
	var acts *Activities
	var edits []queuedEdit
	h.env.OnActivity(acts.RecordQueuedEdit, mock.Anything, mock.Anything, mock.Anything, mock.Anything, mock.Anything).Return(
		func(_ context.Context, _ *commonv1.TenantContext, sig *workflowv1.EditQueuedTaskSignal, applied bool, reason string) error {
			h.mu.Lock()
			defer h.mu.Unlock()
			edits = append(edits, queuedEdit{sig.GetTaskId(), sig.GetMessage(), applied, reason})
			return nil
		})
	return &edits
}

func (h *harness) editQueued(n int, message string) {
	h.signal(SignalEditQueuedTask, &workflowv1.EditQueuedTaskSignal{TaskId: taskID(n), Message: message, ByUserId: "u1", At: timestamppb.New(h.env.Now())})
}

func TestEditingAQueuedTaskReplacesItsMessageInPlace(t *testing.T) {
	h := newHarness(t, defaultCfg())
	edits := recordEdits(h)
	h.at(time.Minute, func() {
		h.submit(submit(1, "c1", write, 10, ""), submit(2, "c2", write, 10, ""), submit(3, "c3", write, 10, ""))
	})
	h.at(5*time.Minute, func() { h.editQueued(2, "改过的消息") })
	h.at(6*time.Minute, func() {
		eq(t, "队列的顺序不变", h.state().GetQueuedTaskIds(), ids(2, 3))
	})
	h.runExpectingCancel(newInput(), 2*time.Hour)
	eq(t, "启动顺序", h.startOrder(), ids(1, 2, 3))
	if got := h.childInput[taskID(2)].GetMessage(); got != "改过的消息" {
		t.Errorf("Task 启动时用改过的消息，实际 %q", got)
	}
	if got := h.childInput[taskID(3)].GetMessage(); got == "改过的消息" {
		t.Error("别的 Task 不受影响")
	}
	if len(*edits) != 1 || !(*edits)[0].applied || (*edits)[0].taskID != taskID(2) || (*edits)[0].message != "改过的消息" {
		t.Errorf("要记下改好了：%+v", *edits)
	}
}

func TestTheLastEditWins(t *testing.T) {
	h := newHarness(t, defaultCfg())
	edits := recordEdits(h)
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 10, ""), submit(2, "c2", write, 10, "")) })
	h.at(2*time.Minute, func() { h.editQueued(2, "第一次改") })
	h.at(3*time.Minute, func() { h.editQueued(2, "第二次改") })
	h.runExpectingCancel(newInput(), 2*time.Hour)
	if got := h.childInput[taskID(2)].GetMessage(); got != "第二次改" {
		t.Errorf("实际 %q", got)
	}
	if len(*edits) != 2 {
		t.Errorf("每次都要告诉客户端：%+v", *edits)
	}
}

func TestEditingARunningTaskChangesNothingAndSaysSo(t *testing.T) {
	h := newHarness(t, defaultCfg())
	edits := recordEdits(h)
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 30, "")) })
	h.at(5*time.Minute, func() { h.editQueued(1, "来晚了") })
	h.runExpectingCancel(newInput(), 2*time.Hour)
	if got := h.childInput[taskID(1)].GetMessage(); got == "来晚了" {
		t.Error("已经开始的 Task 不改")
	}
	if len(*edits) != 1 || (*edits)[0].applied || (*edits)[0].reason != "task_started" {
		t.Errorf("要告诉客户端改晚了：%+v", *edits)
	}
}

func TestEditingAnUnknownOrFinishedTaskIsRejected(t *testing.T) {
	h := newHarness(t, defaultCfg())
	edits := recordEdits(h)
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 5, "")) })
	h.at(20*time.Minute, func() { h.editQueued(1, "已经结束了") })
	h.at(21*time.Minute, func() { h.editQueued(9, "没有这个 Task") })
	h.runExpectingCancel(newInput(), 2*time.Hour)
	if len(*edits) != 2 || (*edits)[0].applied || (*edits)[1].applied {
		t.Errorf("都要被拒绝：%+v", *edits)
	}
	for _, e := range *edits {
		if e.reason == "" {
			t.Errorf("被拒绝要有原因：%+v", e)
		}
	}
}

func TestAnEditThenCancelStillCancels(t *testing.T) {
	h := newHarness(t, defaultCfg())
	recordEdits(h)
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 30, ""), submit(2, "c2", write, 5, "")) })
	h.at(2*time.Minute, func() { h.editQueued(2, "改过的") })
	h.at(3*time.Minute, func() { h.cancel(2) })
	h.runExpectingCancel(newInput(), 2*time.Hour)
	eq(t, "子工作流", h.startOrder(), ids(1))
	h.finishedOnce(2, commonv1.TaskStatus_TASK_STATUS_CANCELLED)
}

func TestAnEditSurvivesContinueAsNew(t *testing.T) {
	cfg := defaultCfg()
	cfg.ContinueAsNewTasks = 3
	h := newHarness(t, cfg)
	recordEdits(h)
	h.at(time.Minute, func() { h.submit(submit(1, "c1", write, 60, ""), submit(2, "c2", write, 60, "")) })
	h.at(2*time.Minute, func() { h.editQueued(2, "改过的消息") })
	h.at(3*time.Minute, func() { h.submit(submit(3, "c3", write, 60, "")) })
	next := h.runExpectingContinueAsNew(newInput(), 2*time.Hour)
	found := false
	for _, sig := range next.GetPending() {
		if sig.GetTask().GetTaskId() == taskID(2) {
			found = true
			if sig.GetTask().GetMessage() != "改过的消息" {
				t.Errorf("续接时带上改过的消息，实际 %q", sig.GetTask().GetMessage())
			}
		}
	}
	if !found {
		t.Fatalf("续接的输入里没有 Task 2：%v", next.GetPending())
	}
}

// --- RecordQueuedEdit 活动 ---

func editedEvents(t *testing.T, f *actFixture, tenant *commonv1.TenantContext, id string) []*taskapiv1.TaskEvent {
	t.Helper()
	var out []*taskapiv1.TaskEvent
	for _, ev := range f.events(t, tenant, id) {
		if ev.GetType() == taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_MESSAGE_EDITED {
			out = append(out, ev)
		}
	}
	return out
}

func TestRecordQueuedEditUpdatesTheMessageAndPublishes(t *testing.T) {
	f := newActFixture(t)
	tenant := testTenant()
	f.insertTask(t, tenant, taskID(1))
	sig := &workflowv1.EditQueuedTaskSignal{TaskId: taskID(1), Message: "改过的消息", ByUserId: "u1"}
	if err := f.acts.RecordQueuedEdit(context.Background(), tenant, sig, true, ""); err != nil {
		t.Fatalf("记下修改：%v", err)
	}
	row, ok, err := f.tasks.LookupTask(context.Background(), tenant, taskID(1))
	if err != nil || !ok {
		t.Fatalf("查不到 Task：%v", err)
	}
	if row.Message != "改过的消息" {
		t.Errorf("业务库的 message 要同步，实际 %q", row.Message)
	}
	evs := editedEvents(t, f, tenant, taskID(1))
	if len(evs) != 1 {
		t.Fatalf("应推一个事件，实际 %d 个", len(evs))
	}
	fields := evs[0].GetPayload().GetValue().GetStructValue().GetFields()
	if fields["event"].GetStringValue() != "task_message_edited" || fields["state"].GetStringValue() != "applied" {
		t.Errorf("载荷不对：%v", fields)
	}
	if _, has := fields["message"]; has {
		t.Error("事件不带消息的文字")
	}
}

func TestEveryEditIsPublishedEvenThoughTheOtherSessionEventsAreOnce(t *testing.T) {
	// publishOnce 按事件类型去重（task_accepted、task_finished 只有一次）；修改可以有好几次。
	f := newActFixture(t)
	tenant := testTenant()
	f.insertTask(t, tenant, taskID(1))
	for _, m := range []string{"第一次", "第二次"} {
		sig := &workflowv1.EditQueuedTaskSignal{TaskId: taskID(1), Message: m, At: timestamppb.New(time.Unix(int64(len(m)), 0))}
		if err := f.acts.RecordQueuedEdit(context.Background(), tenant, sig, true, ""); err != nil {
			t.Fatalf("记下修改：%v", err)
		}
	}
	if n := len(editedEvents(t, f, tenant, taskID(1))); n != 2 {
		t.Errorf("两次修改应推两个事件，实际 %d 个", n)
	}
}

func TestARetriedRecordQueuedEditPublishesOnce(t *testing.T) {
	f := newActFixture(t)
	tenant := testTenant()
	f.insertTask(t, tenant, taskID(1))
	sig := &workflowv1.EditQueuedTaskSignal{TaskId: taskID(1), Message: "改过的", At: timestamppb.New(time.Unix(100, 0))}
	for range 2 {
		if err := f.acts.RecordQueuedEdit(context.Background(), tenant, sig, true, ""); err != nil {
			t.Fatalf("记下修改：%v", err)
		}
	}
	if n := len(editedEvents(t, f, tenant, taskID(1))); n != 1 {
		t.Errorf("活动重试不重复推，实际 %d 个", n)
	}
}

func TestARejectedEditKeepsTheMessageAndSaysWhy(t *testing.T) {
	f := newActFixture(t)
	tenant := testTenant()
	f.insertTask(t, tenant, taskID(1))
	before, _, err := f.tasks.LookupTask(context.Background(), tenant, taskID(1))
	if err != nil {
		t.Fatal(err)
	}
	sig := &workflowv1.EditQueuedTaskSignal{TaskId: taskID(1), Message: "来晚了"}
	if err := f.acts.RecordQueuedEdit(context.Background(), tenant, sig, false, "task_started"); err != nil {
		t.Fatalf("记下被拒绝：%v", err)
	}
	after, _, err := f.tasks.LookupTask(context.Background(), tenant, taskID(1))
	if err != nil {
		t.Fatal(err)
	}
	if after.Message != before.Message {
		t.Errorf("被拒绝不改消息：%q → %q", before.Message, after.Message)
	}
	fields := editedEvents(t, f, tenant, taskID(1))[0].GetPayload().GetValue().GetStructValue().GetFields()
	if fields["state"].GetStringValue() != "rejected" || fields["reason_code"].GetStringValue() != "task_started" {
		t.Errorf("载荷不对：%v", fields)
	}
}

func TestTheTaskWorkflowCannotPublishTheSessionsEvent(t *testing.T) {
	f := newActFixture(t)
	tenant := testTenant()
	f.insertTask(t, tenant, taskID(1))
	ev := &taskapiv1.TaskEvent{TaskId: taskID(1), Type: taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_MESSAGE_EDITED}
	if _, err := f.acts.PublishTaskEvent(context.Background(), tenant, "k1", ev); err == nil {
		t.Error("task_message_edited 只能由 Session 推送")
	}
}

func TestTheTaskWorkflowCanPublishTheNewProgressEvents(t *testing.T) {
	f := newActFixture(t)
	tenant := testTenant()
	f.insertTask(t, tenant, taskID(1))
	for i, typ := range []taskapiv1.TaskEventType{taskapiv1.TaskEventType_TASK_EVENT_TYPE_GUIDANCE_UPDATED, taskapiv1.TaskEventType_TASK_EVENT_TYPE_CONTEXT_USAGE} {
		ev := &taskapiv1.TaskEvent{TaskId: taskID(1), Type: typ}
		if _, err := f.acts.PublishTaskEvent(context.Background(), tenant, string(rune('a'+i)), ev); err != nil {
			t.Errorf("%v：%v", typ, err)
		}
	}
}

func TestTheNewEventsHaveTheirSSENames(t *testing.T) {
	cases := map[taskapiv1.TaskEventType]string{
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_GUIDANCE_UPDATED:    "guidance_updated",
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_MESSAGE_EDITED: "task_message_edited",
		taskapiv1.TaskEventType_TASK_EVENT_TYPE_CONTEXT_USAGE:       "context_usage",
	}
	for typ, want := range cases {
		if got := events.EventName(typ); got != want {
			t.Errorf("%v 的 SSE 事件名应是 %s，实际 %s", typ, want, got)
		}
	}
}
