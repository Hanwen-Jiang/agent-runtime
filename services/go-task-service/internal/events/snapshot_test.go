package events

// N-19 的会话快照与 SSE 游标过期（契约清单 tests/contract/fixtures/n19_n20_contract_cases.json：snapshot_has_cursor、
// delta_starts_after_cursor、reconnect_resume、expired_cursor_requests_snapshot、duplicate_event_is_idempotent、
// tenant_and_session_scope、secret_free_payload；schemas/session_snapshot.schema.json）。

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

func snapshotPath(sessionID, query string) string {
	return "/v1/sessions/" + sessionID + "/snapshot" + query
}

func (f *sseFixture) withSession() *sseFixture {
	f.t.Helper()
	if err := taskstore.New(f.store).CreateSession(context.Background(), tenant("t1"), testSession, f.clock.Now()); err != nil {
		f.t.Fatal(err)
	}
	return f
}

func (f *sseFixture) get(p, token string, header map[string]string) (int, []byte) {
	f.t.Helper()
	req, _ := http.NewRequest(http.MethodGet, f.url+p, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for k, v := range header {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		f.t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, body
}

func (f *sseFixture) snapshot(query string) SessionSnapshot {
	f.t.Helper()
	code, body := f.get(snapshotPath(testSession, query), f.ingress.IssueToken("t1", "u1", time.Hour), nil)
	if code != http.StatusOK {
		f.t.Fatalf("快照应返回 200，实际 %d：%s", code, body)
	}
	var out SessionSnapshot
	if err := json.Unmarshal(body, &out); err != nil {
		f.t.Fatal(err)
	}
	return out
}

func eventIDs(t *testing.T, raw []json.RawMessage) []int64 {
	t.Helper()
	var ids []int64
	for _, r := range raw {
		var ev struct {
			EventID int64 `json:"event_id,string"`
		}
		if err := json.Unmarshal(r, &ev); err != nil {
			t.Fatal(err)
		}
		ids = append(ids, ev.EventID)
	}
	return ids
}

func plan() map[string]any {
	return map[string]any{"event": "plan_updated", "round_seq": 1, "current_step": "a", "steps": []any{
		map[string]any{"id": "a", "title": "读代码", "status": "active", "round": 1},
		map[string]any{"id": "b", "title": "改", "status": "pending", "round": 1},
		map[string]any{"id": "c", "title": "测", "status": "pending", "round": 1},
	}}
}

func TestSnapshotHasCursorRecentEventsAndThePlan(t *testing.T) {
	f := newSSE(t, 0).withSession()
	f.appendWithID(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	f.appendWithID(2, taskapiv1.TaskEventType_TASK_EVENT_TYPE_ROUND_STARTED)
	c, _ := f.cache.Bind(tenant("t1"))
	ev := &taskapiv1.TaskEvent{EventId: 3, TaskId: testTask, SessionId: testSession, Type: taskapiv1.TaskEventType_TASK_EVENT_TYPE_PLAN_UPDATED}
	ev.Payload = jsonPayload(t, plan())
	if _, err := Append(context.Background(), c, ev, time.Hour); err != nil {
		t.Fatal(err)
	}
	f.appendWithID(4, taskapiv1.TaskEventType_TASK_EVENT_TYPE_STAGE_STARTED)
	f.appendWithID(5, taskapiv1.TaskEventType_TASK_EVENT_TYPE_NODE_STARTED)

	snap := f.snapshot("?events=2")
	if snap.SessionID != testSession || snap.GeneratedAt == "" || len(snap.Errors) != 0 || len(snap.Tasks) != 1 {
		t.Fatalf("快照的外层不对：%+v", snap)
	}
	task := snap.Tasks[0]
	if task.TaskID != testTask || task.Status != "running" || task.Source != "events" || task.LastEventID != 5 || !task.EventsTruncated {
		t.Fatalf("Task 的摘要不对：%+v", task)
	}
	if got := eventIDs(t, task.Events); len(got) != 2 || got[0] != 4 || got[1] != 5 {
		t.Fatalf("只留最近两条，实际 %v", got)
	}
	if got := eventIDs(t, []json.RawMessage{task.Plan}); got[0] != 3 || !strings.Contains(string(task.Plan), "plan_updated") {
		t.Fatalf("最新的 plan_updated 即使不在最近的事件里也要给，实际 %s", task.Plan)
	}
}

func TestDeltasAfterTheSnapshotCursorAreNeitherLostNorRepeated(t *testing.T) {
	f := newSSE(t, 0).withSession()
	f.appendWithID(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	f.appendWithID(2, taskapiv1.TaskEventType_TASK_EVENT_TYPE_ROUND_STARTED)
	cursor := f.snapshot("").Tasks[0].LastEventID
	f.appendWithID(3, taskapiv1.TaskEventType_TASK_EVENT_TYPE_STAGE_STARTED)
	// 同一个 event_id 重复进流（推送成功、记标记之前崩溃后的重试）：SSE 按 event_id 只推一次之后的。
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), map[string]string{"Last-Event-ID": itoa(cursor)})
	if ev := s.next(nil); ev == nil || ev.ID != 3 {
		t.Fatalf("快照之后的第一个增量应是 3，实际 %+v", ev)
	}
}

func TestAnExpiredCursorAsksForANewSnapshot(t *testing.T) {
	f := newSSE(t, 0).withSession()
	// 1～6 已过保留期，流里最早的是 7。
	f.appendWithID(7, taskapiv1.TaskEventType_TASK_EVENT_TYPE_STAGE_STARTED)
	token := f.ingress.IssueToken("t1", "u1", time.Hour)
	code, body := f.get(path(testTask), token, map[string]string{"Last-Event-ID": "3"})
	if code != http.StatusConflict || !strings.Contains(string(body), `"code":"resnapshot_required"`) {
		t.Fatalf("游标过期应返回 409 resnapshot_required，实际 %d %s", code, body)
	}
	s := f.open(path(testTask), token, map[string]string{"Last-Event-ID": "6"})
	if s.resp.StatusCode != http.StatusOK {
		t.Fatalf("紧接着的游标照常续传，实际 %d", s.resp.StatusCode)
	}
	if ev := s.next(nil); ev == nil || ev.ID != 7 {
		t.Fatalf("应推 7，实际 %+v", ev)
	}
}

func TestALostStreamFallsBackToTheRecordAndIsReported(t *testing.T) {
	f := newSSE(t, 0).withSession()
	tasks := taskstore.New(f.store)
	for range 3 {
		if _, err := tasks.NextEventID(context.Background(), tenant("t1"), testTask); err != nil {
			t.Fatal(err)
		}
	}
	// 业务库分配过 1～3，Cache 重启后流是空的。
	snap := f.snapshot("")
	task := snap.Tasks[0]
	if task.Source != "record" || task.LastEventID != 0 || len(snap.Errors) != 1 || snap.Errors[0].Code != "events_unavailable" {
		t.Fatalf("没结束的 Task 应退回记录、游标为 0 并报告，实际 %+v %+v", task, snap.Errors)
	}
	f.recordFinished(3)
	task = f.snapshot("").Tasks[0]
	if task.Source != "record" || task.Status != "done" || task.LastEventID != 3 || len(task.Events) != 1 {
		t.Fatalf("结束的 Task 退回记录时补发 task_finished，实际 %+v", task)
	}
}

type brokenCache struct{ cache.Provider }

func (brokenCache) Bind(*commonv1.TenantContext) (cache.Cache, error) {
	return nil, errors.New("cache down")
}

func TestAnUnreachableCacheFallsBackForEveryTask(t *testing.T) {
	f := newSSE(t, 0).withSession()
	hs := httptest.NewServer(NewHandler(HandlerDeps{Ingress: f.ingress, Cache: brokenCache{f.cache}, Store: f.store, Clock: f.clock}))
	t.Cleanup(hs.Close)
	f.url = hs.URL
	snap := f.snapshot("")
	if len(snap.Tasks) != 1 || snap.Tasks[0].Source != "record" || len(snap.Errors) != 1 {
		t.Fatalf("Cache 不可用不能给空快照，实际 %+v", snap)
	}
}

func TestAnUndecodableEventIsSkippedAndReported(t *testing.T) {
	f := newSSE(t, 0).withSession()
	f.appendWithID(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	c, _ := f.cache.Bind(tenant("t1"))
	if _, err := c.AppendEvent(context.Background(), StreamName(testTask), []byte("{not json"), time.Hour); err != nil {
		t.Fatal(err)
	}
	f.appendWithID(3, taskapiv1.TaskEventType_TASK_EVENT_TYPE_ROUND_STARTED)
	snap := f.snapshot("")
	task := snap.Tasks[0]
	if task.Source != "events" || task.LastEventID != 3 || len(task.Events) != 2 || len(snap.Errors) != 1 || snap.Errors[0].Code != "event_undecodable" {
		t.Fatalf("解不开的一条跳过并报告，游标照样往后，实际 %+v %+v", task, snap.Errors)
	}
}

func TestAFinishedTaskIsNotSentTwiceAndCarriesItsStatus(t *testing.T) {
	f := newSSE(t, 0).withSession()
	f.appendWithID(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	f.appendWithID(2, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED)
	f.appendWithID(2, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED)
	f.recordFinished(2)
	task := f.snapshot("").Tasks[0]
	if task.Status != "done" || task.LastEventID != 2 || len(task.Events) != 2 {
		t.Fatalf("task_finished 只给一次，实际 %+v", task)
	}
}

func TestTheSnapshotIsScopedToTheTenantAndTheSession(t *testing.T) {
	f := newSSE(t, 0).withSession()
	f.appendWithID(1, taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED)
	if code, _ := f.get(snapshotPath(testSession, ""), "", nil); code != http.StatusUnauthorized {
		t.Errorf("没有令牌应 401，实际 %d", code)
	}
	if code, _ := f.get(snapshotPath(testSession, ""), f.ingress.IssueToken("t2", "u2", time.Hour), nil); code != http.StatusNotFound {
		t.Errorf("别的租户看不到这个 Session，实际 %d", code)
	}
	other := "ses_01J" + strings.Repeat("0", 22) + "X"
	if code, _ := f.get(snapshotPath(other, ""), f.ingress.IssueToken("t1", "u1", time.Hour), nil); code != http.StatusNotFound {
		t.Errorf("不存在的 Session 应 404，实际 %d", code)
	}
	if code, _ := f.get(snapshotPath("nope", ""), f.ingress.IssueToken("t1", "u1", time.Hour), nil); code != http.StatusBadRequest {
		t.Errorf("不合格式的 session_id 应 400，实际 %d", code)
	}
	if code, _ := f.get(snapshotPath(testSession, "?events=0"), f.ingress.IssueToken("t1", "u1", time.Hour), nil); code != http.StatusBadRequest {
		t.Errorf("events=0 应 400，实际 %d", code)
	}
}

func jsonPayload(t *testing.T, v map[string]any) *commonv1.JsonPayload {
	t.Helper()
	value, err := structpb.NewValue(v)
	if err != nil {
		t.Fatal(err)
	}
	return &commonv1.JsonPayload{Value: value}
}

func itoa(n int64) string { return strconv.FormatInt(n, 10) }
