package events

// HIST-01 验收（Runtime 一侧、不涉及契约；docs/plan/workspace-read-latency-and-disk-2026-11-03.md 第 2 节）：事件记在业务库，
// Cache 里的推流丢了（Cache 重启、过了保留期）也读得到。业务库里有这个 Task 的 task_accepted（event_id 1）时，业务库就是完整的：
//   - SSE：没结束的 Task 先从业务库补发 Last-Event-ID 之后的，再接 Cache 的实时推送；不再因为推流丢了要客户端重取快照（409）。
//   - 结束的 Task 从业务库重放全部事件（不再只有 task_finished）。
//   - 会话快照从业务库读（source 是 events，不再退回 record）。
//   - 推流里重复的同一个 event_id 只推一次。
// 业务库里没有这个 Task 的事件（迁移之前开始的 Task）时照旧：推流、退回 record、过期的游标要重取快照。

import (
	"context"
	"net/http"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

// persist 把一条事件记进业务库（Session 的 activity 做的那一步）。
func (f *sseFixture) persist(id int64, typ taskapiv1.TaskEventType) {
	f.t.Helper()
	ev := &taskapiv1.TaskEvent{EventId: id, TaskId: testTask, SessionId: testSession, Type: typ, EmittedAt: timestamppb.New(f.clock.Now())}
	data, err := marshal.Marshal(ev)
	if err != nil {
		f.t.Fatal(err)
	}
	if _, err := taskstore.New(f.store).RecordEvent(context.Background(), tenant("t1"), taskstore.Event{
		TaskID: testTask, EventID: id, DedupeKey: EventName(typ) + itoa(id), SessionID: testSession, Type: EventName(typ), Data: string(data),
		CreatedAt: f.clock.Now(),
	}); err != nil {
		f.t.Fatal(err)
	}
}

func TestRunningTaskIsBackfilledFromTheDatabase(t *testing.T) {
	f := newSSE(t, 0)
	f.persist(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	f.persist(2, taskapiv1.TaskEventType_TASK_EVENT_TYPE_ROUND_STARTED)
	f.persist(3, taskapiv1.TaskEventType_TASK_EVENT_TYPE_NODE_STARTED)
	// Cache 里什么都没有（推流丢了）。原来这里要 409 重取快照。
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), map[string]string{"Last-Event-ID": "1"})
	if s.resp.StatusCode != http.StatusOK {
		t.Fatalf("业务库里有完整的事件，不要求重取快照：%d", s.resp.StatusCode)
	}
	if a, b := s.next(nil), s.next(nil); a == nil || b == nil || a.ID != 2 || b.ID != 3 {
		t.Fatalf("先从业务库补发 2、3：%+v %+v", a, b)
	}
	go func() {
		time.Sleep(50 * time.Millisecond)
		f.persist(4, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED)
		f.appendWithID(3, taskapiv1.TaskEventType_TASK_EVENT_TYPE_NODE_STARTED) // 推流里重复的旧事件不再推
		f.appendWithID(4, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED)
	}()
	if ev := s.next(nil); ev == nil || ev.ID != 4 || ev.Event != "task_finished" {
		t.Fatalf("再接 Cache 的实时推送：%+v", ev)
	}
	if s.next(nil) != nil {
		t.Fatal("推完 task_finished 结束")
	}
}

func TestDuplicateLiveEventsAreSentOnce(t *testing.T) {
	f := newSSE(t, 0)
	f.persist(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	f.appendWithID(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	f.appendWithID(2, taskapiv1.TaskEventType_TASK_EVENT_TYPE_ROUND_STARTED)
	f.appendWithID(2, taskapiv1.TaskEventType_TASK_EVENT_TYPE_ROUND_STARTED) // 推送成功、记标记之前崩溃，重试又推了一次
	f.appendWithID(3, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED)
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), nil)
	var got []int64
	for ev := s.next(nil); ev != nil; ev = s.next(nil) {
		got = append(got, ev.ID)
	}
	if len(got) != 3 || got[0] != 1 || got[1] != 2 || got[2] != 3 {
		t.Fatalf("同一个 event_id 只推一次：%v", got)
	}
}

func TestFinishedTaskReplaysItsWholeHistoryFromTheDatabase(t *testing.T) {
	f := newSSE(t, 0)
	f.persist(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	f.persist(2, taskapiv1.TaskEventType_TASK_EVENT_TYPE_ROUND_STARTED)
	f.persist(3, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED)
	f.recordFinished(3)
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), nil)
	var got []string
	for ev := s.next(nil); ev != nil; ev = s.next(nil) {
		got = append(got, itoa(ev.ID)+":"+ev.Event)
	}
	if len(got) != 3 || got[0] != "1:task_accepted" || got[1] != "2:round_started" || got[2] != "3:task_finished" {
		t.Fatalf("推流丢了也从业务库重放全部事件：%v", got)
	}
}

func TestSnapshotReadsTheDatabase(t *testing.T) {
	f := newSSE(t, 0).withSession()
	f.persist(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	f.persist(2, taskapiv1.TaskEventType_TASK_EVENT_TYPE_ROUND_STARTED)
	f.persist(3, taskapiv1.TaskEventType_TASK_EVENT_TYPE_NODE_STARTED)
	snap := f.snapshot("?events=2")
	if len(snap.Errors) != 0 || len(snap.Tasks) != 1 {
		t.Fatalf("快照：%+v", snap)
	}
	task := snap.Tasks[0]
	ids := eventIDs(t, task.Events)
	if task.Source != "events" || task.LastEventID != 3 || !task.EventsTruncated || len(ids) != 2 || ids[0] != 2 || ids[1] != 3 {
		t.Fatalf("推流丢了也从业务库给出最近的事件：%+v %v", task, ids)
	}
}

func TestTasksWithoutPersistedEventsKeepTheOldFallback(t *testing.T) {
	f := newSSE(t, 0).withSession()
	for range 2 {
		if _, err := taskstore.New(f.store).NextEventID(context.Background(), tenant("t1"), testTask); err != nil {
			t.Fatal(err)
		}
	}
	// 迁移之前开始的 Task：业务库记着分配到 2，事件不在业务库里，推流也丢了。
	snap := f.snapshot("")
	if snap.Tasks[0].Source != "record" {
		t.Fatalf("业务库里没有事件时照旧退回 record：%+v", snap.Tasks[0])
	}
	code, _ := f.get(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), map[string]string{"Last-Event-ID": "1"})
	if code != http.StatusConflict {
		t.Fatalf("照旧要求重取快照，实际 %d", code)
	}
}
