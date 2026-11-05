package session

// HIST-01 验收（Runtime 一侧、不涉及契约；docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：事件先写业务库、
// 再写 Cache 的推流。
//   - 受理、进度、结束的每个事件都按（task_id、event_id）记进业务库的 task_events，内容与推流里的一样。
//   - 去重以业务库为准：Cache 的标记丢了（Cache 重启）再重试，拿到的还是原来的 event_id，业务库里不多一行。
//   - 写进业务库之后写 Cache 失败：activity 报可重试的错误，重试沿用同一个 event_id。

import (
	"context"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

func (f *actFixture) persisted(t *testing.T, tenant *commonv1.TenantContext, id string) []*taskapiv1.TaskEvent {
	t.Helper()
	rows, err := f.tasks.ListEvents(context.Background(), tenant, id, 0, 100)
	if err != nil {
		t.Fatal(err)
	}
	var out []*taskapiv1.TaskEvent
	for _, r := range rows {
		ev := &taskapiv1.TaskEvent{}
		if err := protojson.Unmarshal([]byte(r.Data), ev); err != nil {
			t.Fatal(err)
		}
		if ev.GetEventId() != r.EventID {
			t.Fatalf("行的 event_id %d 与内容里的 %d 不一致", r.EventID, ev.GetEventId())
		}
		out = append(out, ev)
	}
	return out
}

func nodeStarted(id string) *taskapiv1.TaskEvent {
	return &taskapiv1.TaskEvent{TaskId: id, Type: taskapiv1.TaskEventType_TASK_EVENT_TYPE_NODE_STARTED, EmittedAt: timestamppb.Now()}
}

func TestEveryEventIsPersistedLikeTheStream(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(11)
	f.insertTask(t, testTenant(), id)
	if err := f.acts.AcceptTask(ctx, testTenant(), id); err != nil {
		t.Fatal(err)
	}
	if _, err := f.acts.PublishTaskEvent(ctx, testTenant(), "k1", nodeStarted(id)); err != nil {
		t.Fatal(err)
	}
	if err := f.acts.FinishTask(ctx, testTenant(), &workflowv1.TaskWorkflowResult{TaskId: id, Status: commonv1.TaskStatus_TASK_STATUS_DONE}); err != nil {
		t.Fatal(err)
	}
	stored, streamed := f.persisted(t, testTenant(), id), f.events(t, testTenant(), id)
	if len(stored) != 3 || len(streamed) != 3 {
		t.Fatalf("业务库 %d 条、推流 %d 条，都应是 3 条", len(stored), len(streamed))
	}
	for i := range stored {
		a, _ := protojson.Marshal(stored[i])
		b, _ := protojson.Marshal(streamed[i])
		if string(a) != string(b) || stored[i].GetEventId() != int64(i+1) {
			t.Fatalf("第 %d 条不一致：%s / %s", i+1, a, b)
		}
	}
	if other := f.persisted(t, &commonv1.TenantContext{TenantId: "t2", UserId: "u9", SessionId: testSession}, id); len(other) != 0 {
		t.Fatalf("别的租户读到了 %d 条", len(other))
	}
}

func TestRetryAfterTheCacheRestartReusesTheEventID(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(12)
	f.insertTask(t, testTenant(), id)
	if err := f.acts.AcceptTask(ctx, testTenant(), id); err != nil {
		t.Fatal(err)
	}
	first, err := f.acts.PublishTaskEvent(ctx, testTenant(), "k1", nodeStarted(id))
	if err != nil {
		t.Fatal(err)
	}
	f.cache = memcache.New(memcache.Options{Clock: f.clock}) // Cache 重启，去重标记没了
	f.acts.Cache = f.cache
	again, err := f.acts.PublishTaskEvent(ctx, testTenant(), "k1", nodeStarted(id))
	if err != nil {
		t.Fatal(err)
	}
	if again != first {
		t.Fatalf("重试要沿用业务库里的 event_id %d，实际 %d", first, again)
	}
	if n := len(f.persisted(t, testTenant(), id)); n != 2 {
		t.Fatalf("业务库里应只有受理与这一条，实际 %d 条", n)
	}
	if evs := f.events(t, testTenant(), id); len(evs) != 1 || evs[0].GetEventId() != first {
		t.Fatalf("新的推流里补上这一条（同一个 event_id）：%v", evs)
	}
	if err := f.acts.AcceptTask(ctx, testTenant(), id); err != nil {
		t.Fatal(err)
	}
	if n := len(f.persisted(t, testTenant(), id)); n != 2 {
		t.Fatalf("受理重试也不多一行，实际 %d 条", n)
	}
}

func TestCacheFailureAfterRecordingIsRetriedWithTheSameID(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	faults := testkit.NewFaults()
	f.cache = memcache.New(memcache.Options{Clock: f.clock, Faults: faults})
	f.acts.Cache = f.cache
	id := taskID(13)
	f.insertTask(t, testTenant(), id)
	if err := f.acts.AcceptTask(ctx, testTenant(), id); err != nil {
		t.Fatal(err)
	}
	faults.FailNext("AppendEvent", providererr.Transient)
	_, err := f.acts.PublishTaskEvent(ctx, testTenant(), "k1", nodeStarted(id))
	if err == nil || !providererr.IsRetryable(err) {
		t.Fatalf("写 Cache 失败要报可重试的错误：%v", err)
	}
	stored := f.persisted(t, testTenant(), id)
	if len(stored) != 2 {
		t.Fatalf("先写业务库：失败之前已记下，实际 %d 条", len(stored))
	}
	got, err := f.acts.PublishTaskEvent(ctx, testTenant(), "k1", nodeStarted(id))
	if err != nil {
		t.Fatal(err)
	}
	if got != stored[1].GetEventId() || len(f.persisted(t, testTenant(), id)) != 2 {
		t.Fatalf("重试沿用 event_id %d，实际 %d", stored[1].GetEventId(), got)
	}
}

// 逐字的增量（token_delta、thinking_delta）只走推流、不进业务库：量大，历史里有节点与 Task 的结果就够了。
func TestStreamingDeltasAreNotPersisted(t *testing.T) {
	f := newActFixture(t)
	ctx := context.Background()
	id := taskID(14)
	f.insertTask(t, testTenant(), id)
	if err := f.acts.AcceptTask(ctx, testTenant(), id); err != nil {
		t.Fatal(err)
	}
	delta := &taskapiv1.TaskEvent{TaskId: id, Type: taskapiv1.TaskEventType_TASK_EVENT_TYPE_TOKEN_DELTA, EmittedAt: timestamppb.Now()}
	if _, err := f.acts.PublishTaskEvent(ctx, testTenant(), "d1", delta); err != nil {
		t.Fatal(err)
	}
	if n := len(f.persisted(t, testTenant(), id)); n != 1 {
		t.Fatalf("增量不进业务库，实际 %d 条", n)
	}
	if n := len(f.events(t, testTenant(), id)); n != 2 {
		t.Fatalf("推流里照常有，实际 %d 条", n)
	}
}
