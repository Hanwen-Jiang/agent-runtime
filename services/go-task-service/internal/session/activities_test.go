package session

// S-03 验收：Session 工作流的 activity。端到端验收要求“SSE 收到结果”，事件经 Cache 推流（附录 K.1“流式输出”）；
// activity 可能被重试，推送必须幂等；每次 Provider 调用都带租户上下文。

import (
	"context"
	"errors"
	"testing"
	"time"

	"go.temporal.io/sdk/temporal"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

type actFixture struct {
	acts  *Activities
	cache *memcache.Provider
	store *sqlitestore.Provider
	tasks *taskstore.Store
	clock *testkit.FakeClock
}

func newActFixture(t *testing.T) *actFixture {
	t.Helper()
	clk := testkit.NewFakeClock()
	c := memcache.New(memcache.Options{Clock: clk})
	s, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	if err := s.Migrate(context.Background(), taskstore.Migrations()); err != nil {
		t.Fatal(err)
	}
	return &actFixture{
		acts:  &Activities{Cache: c, Store: s, Clock: clk, Retention: time.Hour},
		cache: c, store: s, tasks: taskstore.New(s), clock: clk,
	}
}

func (f *actFixture) insertTask(t *testing.T, tenant *commonv1.TenantContext, id string) {
	t.Helper()
	if _, _, err := f.tasks.InsertTask(context.Background(), tenant, taskstore.Task{
		TaskID: id, SessionID: tenant.GetSessionId(), ClientRequestID: "c-" + id, CreatedAt: f.clock.Now(),
	}); err != nil {
		t.Fatal(err)
	}
}

func (f *actFixture) events(t *testing.T, tenant *commonv1.TenantContext, id string) []*taskapiv1.TaskEvent {
	t.Helper()
	c, err := f.cache.Bind(tenant)
	if err != nil {
		t.Fatal(err)
	}
	page, err := c.ReadEvents(context.Background(), events.StreamName(id), 0, 100)
	if err != nil {
		t.Fatal(err)
	}
	var out []*taskapiv1.TaskEvent
	for _, se := range page.Events {
		ev, err := events.Decode(se)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, ev)
	}
	return out
}

func TestAcceptTaskPublishesTaskAcceptedOnce(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(1)
	f.insertTask(t, testTenant(), id)
	for range 2 { // activity 重试
		if err := f.acts.AcceptTask(ctx, testTenant(), id); err != nil {
			t.Fatal(err)
		}
	}
	evs := f.events(t, testTenant(), id)
	if len(evs) != 1 {
		t.Fatalf("重复调用只推一次 TASK_ACCEPTED，实际 %d 个事件", len(evs))
	}
	ev := evs[0]
	if ev.GetType() != taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED || ev.GetEventId() != 1 ||
		ev.GetTaskId() != id || ev.GetSessionId() != testSession || !ev.GetEmittedAt().AsTime().Equal(f.clock.Now()) {
		t.Errorf("TASK_ACCEPTED 事件不对：%v", ev)
	}
}

func TestFinishTaskPublishesResultAndRecordsStatus(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(2)
	f.insertTask(t, testTenant(), id)
	if err := f.acts.AcceptTask(ctx, testTenant(), id); err != nil {
		t.Fatal(err)
	}
	answer, _ := structpb.NewValue(map[string]any{"text": "42"})
	result := &workflowv1.TaskWorkflowResult{
		TaskId: id, Status: commonv1.TaskStatus_TASK_STATUS_DONE, FinalVerdict: commonv1.Verdict_VERDICT_DONE,
		DeliverNodeId: "answer", Deliverable: &commonv1.JsonPayload{Value: answer}, Rounds: 1,
	}
	for range 2 {
		if err := f.acts.FinishTask(ctx, testTenant(), result); err != nil {
			t.Fatal(err)
		}
	}
	evs := f.events(t, testTenant(), id)
	if len(evs) != 2 {
		t.Fatalf("应有 TASK_ACCEPTED 与一个 TASK_FINISHED，实际 %d 个事件", len(evs))
	}
	fin := evs[1]
	if fin.GetType() != taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED || fin.GetEventId() != 2 {
		t.Fatalf("第二个事件应为 TASK_FINISHED（event_id 2），实际 %v", fin)
	}
	fields := fin.GetPayload().GetValue().GetStructValue().GetFields()
	if fields["task_id"].GetStringValue() != id || fields["status"].GetStringValue() != "TASK_STATUS_DONE" {
		t.Errorf("TASK_FINISHED 的载荷应是 Task 的结局（proto 字段名），实际 %v", fin.GetPayload())
	}
	if fields["deliverable"].GetStructValue().GetFields()["value"].GetStructValue().GetFields()["text"].GetStringValue() != "42" {
		t.Errorf("TASK_FINISHED 的载荷应带交付内容，实际 %v", fin.GetPayload())
	}

	row, ok, err := f.tasks.LookupTask(ctx, testTenant(), id)
	if err != nil || !ok {
		t.Fatalf("找不到 Task 记录：ok=%v err=%v", ok, err)
	}
	if row.Status != commonv1.TaskStatus_TASK_STATUS_DONE || row.LastEventID != 2 {
		t.Errorf("业务库应记下终态 DONE 与最后的 event_id 2，实际 %+v", row)
	}
}

func TestActivityEventsAreTenantScoped(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(3)
	f.insertTask(t, testTenant(), id)
	if err := f.acts.AcceptTask(ctx, testTenant(), id); err != nil {
		t.Fatal(err)
	}
	other := &commonv1.TenantContext{TenantId: "t2", UserId: "u9", SessionId: testSession}
	if evs := f.events(t, other, id); len(evs) != 0 {
		t.Errorf("租户 t2 不应读到租户 t1 的事件，实际 %d 个", len(evs))
	}
}

func TestActivitiesRejectMissingTenantWithoutRetry(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	check := func(name string, err error) {
		t.Helper()
		var app *temporal.ApplicationError
		if !errors.As(err, &app) || !app.NonRetryable() {
			t.Errorf("%s 缺少租户时应返回不可重试的错误，实际 %v", name, err)
		}
	}
	check("AcceptTask", f.acts.AcceptTask(ctx, nil, taskID(4)))
	check("FinishTask", f.acts.FinishTask(ctx, &commonv1.TenantContext{}, &workflowv1.TaskWorkflowResult{TaskId: taskID(4)}))
}
