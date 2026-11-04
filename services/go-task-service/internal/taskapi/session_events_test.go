package taskapi

// HIST-01 的批量取事件 ListSessionEvents（契约 task_api.proto，2026-11-04 人审通过）。

import (
	"context"
	"fmt"
	"testing"

	"connectrpc.com/connect"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

// recordEvents 给 Task 记下这些 event_id 的事件；finished 时最后一个是 task_finished。
func (f *apiFixture) recordEvents(tenant, taskID, sessionID string, finished bool, eventIDs ...int64) {
	f.t.Helper()
	for i, n := range eventIDs {
		typ := taskapiv1.TaskEventType_TASK_EVENT_TYPE_NODE_STARTED
		if n == 1 {
			typ = taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED
		}
		if finished && i == len(eventIDs)-1 {
			typ = taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED
		}
		data, err := events.Encode(&taskapiv1.TaskEvent{EventId: n, TaskId: taskID, SessionId: sessionID, Type: typ})
		if err != nil {
			f.t.Fatal(err)
		}
		_, err = f.tasks.RecordEvent(context.Background(), &commonv1.TenantContext{TenantId: tenant}, taskstore.Event{
			TaskID: taskID, EventID: n, DedupeKey: fmt.Sprintf("seed-%d", n), SessionID: sessionID, Type: events.EventName(typ), Data: string(data),
		})
		if err != nil {
			f.t.Fatal(err)
		}
	}
}

func (f *apiFixture) sessionEvents(token string, req *taskapiv1.ListSessionEventsRequest) (*taskapiv1.ListSessionEventsResponse, error) {
	resp, err := f.client.ListSessionEvents(context.Background(), authed(token, req))
	if err != nil {
		return nil, err
	}
	return resp.Msg, nil
}

func returned(te *taskapiv1.TaskEvents) []int64 {
	var out []int64
	for _, ev := range te.GetEvents() {
		out = append(out, ev.GetEventId())
	}
	return out
}

func cursor(taskID string, after int64) *taskapiv1.TaskEventCursor {
	return &taskapiv1.TaskEventCursor{TaskId: taskID, AfterEventId: after}
}

func TestSessionEventsFollowEachCursor(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	a := f.mustSubmit(tok, newRequest("a"))
	second := newRequest("b")
	second.SessionId = a.GetSessionId()
	b := f.mustSubmit(tok, second)
	elsewhere := f.mustSubmit(tok, newRequest("c"))               // 另一个 Session
	foreign := f.mustSubmit(f.token("t2", "u1"), newRequest("d")) // 另一个租户
	f.recordEvents("t1", a.GetTaskId(), a.GetSessionId(), true, 1, 2, 3)
	f.recordEvents("t1", b.GetTaskId(), a.GetSessionId(), false, 1, 2)
	f.recordEvents("t1", elsewhere.GetTaskId(), elsewhere.GetSessionId(), false, 1)
	f.recordEvents("t2", foreign.GetTaskId(), foreign.GetSessionId(), false, 1)

	resp, err := f.sessionEvents(tok, &taskapiv1.ListSessionEventsRequest{SessionId: a.GetSessionId(), Cursors: []*taskapiv1.TaskEventCursor{
		cursor(b.GetTaskId(), 1), cursor(a.GetTaskId(), 0), cursor(elsewhere.GetTaskId(), 0), cursor(foreign.GetTaskId(), 0),
		cursor("tsk_01J0000000000000000000000Z", 0),
	}})
	if err != nil {
		t.Fatal(err)
	}
	got := resp.GetTasks()
	if len(got) != 5 || got[0].GetTaskId() != b.GetTaskId() || got[1].GetTaskId() != a.GetTaskId() {
		t.Fatalf("与请求的 cursors 一一对应、顺序相同：%v", got)
	}
	if fmt.Sprint(returned(got[0])) != "[2]" || got[0].GetNextAfterEventId() != 2 || got[0].GetFinished() || !got[0].GetFound() {
		t.Errorf("游标之后的事件：%v", got[0])
	}
	if fmt.Sprint(returned(got[1])) != "[1 2 3]" || got[1].GetNextAfterEventId() != 3 || !got[1].GetFinished() || got[1].GetMore() {
		t.Errorf("从头读、task_finished 在库里时 finished：%v", got[1])
	}
	if got[1].GetEvents()[2].GetType() != taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED {
		t.Errorf("事件内容与记下的一样：%v", got[1].GetEvents()[2])
	}
	for i, what := range map[int]string{2: "别的 Session", 3: "别的租户", 4: "不存在"} {
		if got[i].GetFound() || len(got[i].GetEvents()) != 0 || got[i].GetNextAfterEventId() != 0 {
			t.Errorf("%s的 Task 当作不存在：%v", what, got[i])
		}
	}
}

func TestSessionEventsPageWithMore(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	a := f.mustSubmit(tok, newRequest("a"))
	f.recordEvents("t1", a.GetTaskId(), a.GetSessionId(), false, 1, 2, 3, 4, 5)
	var after int64
	var pages []string
	for range 4 {
		resp, err := f.sessionEvents(tok, &taskapiv1.ListSessionEventsRequest{SessionId: a.GetSessionId(), PageSize: 2, Cursors: []*taskapiv1.TaskEventCursor{cursor(a.GetTaskId(), after)}})
		if err != nil {
			t.Fatal(err)
		}
		te := resp.GetTasks()[0]
		pages = append(pages, fmt.Sprintf("%v more=%v", returned(te), te.GetMore()))
		after = te.GetNextAfterEventId()
		if !te.GetMore() {
			break
		}
	}
	if want := "[[1 2] more=true [3 4] more=true [5] more=false]"; fmt.Sprint(pages) != want {
		t.Fatalf("分页不重不漏：%v，应为 %v", pages, want)
	}
}

func TestSessionEventsWithoutTheFirstEventAreUnavailable(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	a := f.mustSubmit(tok, newRequest("a"))
	f.recordEvents("t1", a.GetTaskId(), a.GetSessionId(), false, 4, 5) // 迁移之前开始的 Task：库里没有第 1 个事件
	resp, err := f.sessionEvents(tok, &taskapiv1.ListSessionEventsRequest{SessionId: a.GetSessionId(), Cursors: []*taskapiv1.TaskEventCursor{cursor(a.GetTaskId(), 0)}})
	if err != nil {
		t.Fatal(err)
	}
	if te := resp.GetTasks()[0]; !te.GetFound() || !te.GetEventsUnavailable() || len(te.GetEvents()) != 0 {
		t.Fatalf("退回快照或 SSE：%v", te)
	}
}

func TestSessionEventsRejectBadRequests(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	a := f.mustSubmit(tok, newRequest("a"))
	many := make([]*taskapiv1.TaskEventCursor, 51)
	for i := range many {
		many[i] = cursor(fmt.Sprintf("tsk_01J00000000000000000000%03d", i), 0)
	}
	session := a.GetSessionId()
	for what, req := range map[string]*taskapiv1.ListSessionEventsRequest{
		"session_id 格式不对":   {SessionId: "nope", Cursors: []*taskapiv1.TaskEventCursor{cursor(a.GetTaskId(), 0)}},
		"超过 50 个 Task":      {SessionId: session, Cursors: many},
		"同一个 Task 两次":       {SessionId: session, Cursors: []*taskapiv1.TaskEventCursor{cursor(a.GetTaskId(), 0), cursor(a.GetTaskId(), 3)}},
		"游标为负":              {SessionId: session, Cursors: []*taskapiv1.TaskEventCursor{cursor(a.GetTaskId(), -1)}},
		"task_id 格式不对":      {SessionId: session, Cursors: []*taskapiv1.TaskEventCursor{cursor("x", 0)}},
		"page_size 超过 1000": {SessionId: session, PageSize: 1001, Cursors: []*taskapiv1.TaskEventCursor{cursor(a.GetTaskId(), 0)}},
	} {
		_, err := f.sessionEvents(tok, req)
		wantCode(t, what, err, connect.CodeInvalidArgument)
	}
	_, err := f.sessionEvents(tok, &taskapiv1.ListSessionEventsRequest{TenantId: "t2", SessionId: session})
	wantCode(t, "tenant_id 与认证身份不一致", err, connect.CodePermissionDenied)
	_, err = f.sessionEvents("", &taskapiv1.ListSessionEventsRequest{SessionId: session})
	wantCode(t, "没认证", err, connect.CodeUnauthenticated)
}
