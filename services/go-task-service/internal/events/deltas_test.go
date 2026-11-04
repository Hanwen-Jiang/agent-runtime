package events

// DELTA（docs/plan/all-plans-dag-2026-11-03.md 7.1）在 SSE 里的样子：
//
//  1. 在线的连接收得到连上之后来的增量（event 是 token_delta、thinking_delta），没有 id 行：Last-Event-ID 只跟持久的事件走。
//  2. 连上之前就有的增量不补发：增量只给在线的连接，节点结束时的持久事件带完整结果。
//  3. 关掉了逐字增量的租户收不到增量，持久的事件照常。

import (
	"context"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
)

func (f *sseFixture) pushDelta(text string, offset int) {
	f.t.Helper()
	c, err := f.cache.Bind(tenant("t1"))
	if err != nil {
		f.t.Fatal(err)
	}
	v, _ := structpb.NewValue(map[string]any{"node_id": "answer", "offset": offset, "text": text})
	ev := &taskapiv1.TaskEvent{TaskId: testTask, SessionId: testSession, Type: taskapiv1.TaskEventType_TASK_EVENT_TYPE_TOKEN_DELTA,
		EmittedAt: timestamppb.Now(), Payload: &commonv1.JsonPayload{Value: v}}
	if _, err := AppendDelta(context.Background(), c, ev); err != nil {
		f.t.Fatal(err)
	}
}

// finishLater 稍后追加 task_finished（event_id 2），让流结束。
func (f *sseFixture) finishLater(wait time.Duration) {
	go func() {
		time.Sleep(wait)
		f.appendWithID(2, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED)
	}()
}

func TestLiveDeltasReachConnectedClientsWithoutIDs(t *testing.T) {
	f := newSSE(t, 0)
	f.appendWithID(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), nil)
	if ev := s.next(nil); ev == nil || ev.ID != 1 {
		t.Fatalf("先是持久的事件：%+v", ev)
	}
	go func() {
		time.Sleep(50 * time.Millisecond)
		f.pushDelta("Hel", 0)
		f.pushDelta("lo", 3)
	}()
	a, b := s.next(nil), s.next(nil)
	if a == nil || b == nil || a.Event != "token_delta" || a.ID != 0 || !strings.Contains(a.Data, `"Hel"`) || !strings.Contains(b.Data, `"lo"`) {
		t.Fatalf("连上之后的增量按顺序推，没有 id：%+v %+v", a, b)
	}
	f.finishLater(50 * time.Millisecond)
	if ev := s.next(nil); ev == nil || ev.ID != 2 || ev.Event != "task_finished" {
		t.Fatalf("结束事件照常：%+v", ev)
	}
	if s.next(nil) != nil {
		t.Fatal("推完 task_finished 结束")
	}
}

func TestDeltasFromBeforeTheConnectionAreNotReplayed(t *testing.T) {
	f := newSSE(t, 0)
	f.appendWithID(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	f.pushDelta("old", 0)
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), nil)
	f.finishLater(200 * time.Millisecond)
	var got []string
	for ev := s.next(nil); ev != nil; ev = s.next(nil) {
		got = append(got, ev.Event)
	}
	if strings.Join(got, ",") != "task_accepted,task_finished" {
		t.Fatalf("连上之前的增量不补发：%v", got)
	}
}

func TestATenantWithDeltasOffGetsNone(t *testing.T) {
	f := newSSEWith(t, sseOptions{deltasOff: func(tenantID string) bool { return tenantID == "t1" }})
	f.appendWithID(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), nil)
	if ev := s.next(nil); ev == nil || ev.ID != 1 {
		t.Fatalf("持久的事件照常：%+v", ev)
	}
	go func() {
		time.Sleep(50 * time.Millisecond)
		f.pushDelta("Hel", 0)
	}()
	f.finishLater(200 * time.Millisecond)
	for ev := s.next(nil); ev != nil; ev = s.next(nil) {
		if ev.Event != "task_finished" {
			t.Fatalf("关掉了的租户收不到增量：%+v", ev)
		}
	}
}
