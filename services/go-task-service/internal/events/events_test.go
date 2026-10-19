package events

// S-03 验收：SSE 推送结果（计划 S-03 验收“端到端：……SSE 收到结果”；设计书 4.14：事件带递增 ID，
// 断线后凭 Last-Event-ID 续传，推流数据在 Cache 中设保留时间）。SSE 端点同样按认证身份确定租户。

import (
	"bufio"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

var (
	testSession = "ses_01J" + strings.Repeat("0", 22) + "S"
	testTask    = "tsk_01J" + strings.Repeat("0", 22) + "T"
)

func tenant(id string) *commonv1.TenantContext {
	return &commonv1.TenantContext{TenantId: id, UserId: "u1", SessionId: testSession}
}

type sseFixture struct {
	t       *testing.T
	cache   *memcache.Provider
	ingress *localingress.Provider
	url     string
}

func newSSE(t *testing.T, heartbeat time.Duration) *sseFixture {
	t.Helper()
	clk := testkit.NewFakeClock()
	c := memcache.New(memcache.Options{Clock: clk})
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	ctx := context.Background()
	if err := store.Migrate(ctx, taskstore.Migrations()); err != nil {
		t.Fatal(err)
	}
	if _, _, err := taskstore.New(store).InsertTask(ctx, tenant("t1"), taskstore.Task{
		TaskID: testTask, SessionID: testSession, ClientRequestID: "c1", CreatedAt: clk.Now(),
	}); err != nil {
		t.Fatal(err)
	}
	ing := localingress.New(localingress.Options{SigningKey: []byte("sse-test-key"), Clock: clk})
	hs := httptest.NewServer(NewHandler(HandlerDeps{Ingress: ing, Cache: c, Store: store, Heartbeat: heartbeat}))
	t.Cleanup(hs.Close)
	return &sseFixture{t: t, cache: c, ingress: ing, url: hs.URL}
}

func (f *sseFixture) append(typ taskapiv1.TaskEventType, payload map[string]any) int64 {
	f.t.Helper()
	c, err := f.cache.Bind(tenant("t1"))
	if err != nil {
		f.t.Fatal(err)
	}
	ev := &taskapiv1.TaskEvent{TaskId: testTask, SessionId: testSession, Type: typ, EmittedAt: timestamppb.Now()}
	if payload != nil {
		v, _ := structpb.NewValue(payload)
		ev.Payload = &commonv1.JsonPayload{Value: v}
	}
	id, err := Append(context.Background(), c, ev, time.Hour)
	if err != nil {
		f.t.Fatal(err)
	}
	return id
}

type sseEvent struct {
	ID    int64
	Event string
	Data  string
}

type stream struct {
	resp   *http.Response
	reader *bufio.Reader
	cancel context.CancelFunc
}

func (f *sseFixture) open(path, token string, header map[string]string) *stream {
	f.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, f.url+path, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for k, v := range header {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		cancel()
		f.t.Fatal(err)
	}
	f.t.Cleanup(func() { cancel(); resp.Body.Close() })
	return &stream{resp: resp, reader: bufio.NewReader(resp.Body), cancel: cancel}
}

// next 读出下一个事件；comments 收集期间的注释行。流结束时返回 nil。
func (s *stream) next(comments *[]string) *sseEvent {
	var ev sseEvent
	seen := false
	for {
		line, err := s.reader.ReadString('\n')
		if err == io.EOF && line == "" {
			if seen {
				return &ev
			}
			return nil
		}
		line = strings.TrimRight(line, "\r\n")
		switch {
		case line == "":
			if seen {
				return &ev
			}
		case strings.HasPrefix(line, ":"):
			if comments != nil {
				*comments = append(*comments, line)
			}
		case strings.HasPrefix(line, "id:"):
			ev.ID, _ = strconv.ParseInt(strings.TrimSpace(line[3:]), 10, 64)
			seen = true
		case strings.HasPrefix(line, "event:"):
			ev.Event = strings.TrimSpace(line[6:])
			seen = true
		case strings.HasPrefix(line, "data:"):
			ev.Data += strings.TrimSpace(line[5:])
			seen = true
		}
		if err != nil {
			return &ev
		}
	}
}

func path(taskID string) string { return "/v1/tasks/" + taskID + "/events" }

func TestStreamsEventsUntilTaskFinished(t *testing.T) {
	f := newSSE(t, 0)
	f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED, nil)
	f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED, map[string]any{"status": "TASK_STATUS_DONE"})
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), nil)
	if s.resp.StatusCode != http.StatusOK || !strings.HasPrefix(s.resp.Header.Get("Content-Type"), "text/event-stream") {
		t.Fatalf("应返回 text/event-stream，实际 %d %q", s.resp.StatusCode, s.resp.Header.Get("Content-Type"))
	}
	first, second := s.next(nil), s.next(nil)
	if first == nil || first.ID != 1 || first.Event != "task_accepted" {
		t.Fatalf("第一个事件应为 id 1 的 task_accepted，实际 %+v", first)
	}
	if second == nil || second.ID != 2 || second.Event != "task_finished" {
		t.Fatalf("第二个事件应为 id 2 的 task_finished，实际 %+v", second)
	}
	var ev taskapiv1.TaskEvent
	if err := protojson.Unmarshal([]byte(second.Data), &ev); err != nil {
		t.Fatalf("data 应是 TaskEvent 的 JSON：%v\n%s", err, second.Data)
	}
	if ev.GetEventId() != 2 || ev.GetTaskId() != testTask || ev.GetType() != taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED ||
		ev.GetPayload().GetValue().GetStructValue().GetFields()["status"].GetStringValue() != "TASK_STATUS_DONE" {
		t.Errorf("TaskEvent 内容不对：%v", &ev)
	}
	if s.next(nil) != nil {
		t.Error("推完 task_finished 后应结束推流")
	}
}

func TestStreamDeliversEventsAppendedLater(t *testing.T) {
	f := newSSE(t, 0)
	f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED, nil)
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), nil)
	if ev := s.next(nil); ev == nil || ev.ID != 1 {
		t.Fatalf("应先补发已有事件，实际 %+v", ev)
	}
	go func() {
		time.Sleep(50 * time.Millisecond)
		f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED, nil)
	}()
	if ev := s.next(nil); ev == nil || ev.ID != 2 || ev.Event != "task_finished" {
		t.Fatalf("应推送订阅之后追加的事件，实际 %+v", ev)
	}
}

func TestStreamResumesAfterLastEventID(t *testing.T) {
	f := newSSE(t, 0)
	for range 3 {
		f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_HEARTBEAT, nil)
	}
	f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED, nil)
	tok := f.ingress.IssueToken("t1", "u1", time.Hour)

	s := f.open(path(testTask), tok, map[string]string{"Last-Event-ID": "2"})
	if ev := s.next(nil); ev == nil || ev.ID != 3 {
		t.Errorf("带 Last-Event-ID: 2 应从 id 3 开始，实际 %+v", ev)
	}
	q := f.open(path(testTask)+"?last_event_id=3", tok, nil)
	if ev := q.next(nil); ev == nil || ev.ID != 4 {
		t.Errorf("带 last_event_id=3 查询参数应从 id 4 开始，实际 %+v", ev)
	}
}

func TestStreamSendsHeartbeatsWhileIdle(t *testing.T) {
	f := newSSE(t, 20*time.Millisecond)
	f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED, nil)
	s := f.open(path(testTask), f.ingress.IssueToken("t1", "u1", time.Hour), nil)
	var comments []string
	s.next(&comments) // task_accepted
	deadline := time.Now().Add(2 * time.Second)
	for len(comments) == 0 && time.Now().Before(deadline) {
		line, err := s.reader.ReadString('\n')
		if err != nil {
			break
		}
		if strings.HasPrefix(line, ":") {
			comments = append(comments, line)
		}
	}
	if len(comments) == 0 {
		t.Error("空闲时应定期发注释行，防止代理断开连接")
	}
}

func TestStreamRequiresAuthAndOwnership(t *testing.T) {
	f := newSSE(t, 0)
	f.append(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED, nil)
	cases := []struct {
		name, path, token string
		status            int
	}{
		{"没有凭据", path(testTask), "", http.StatusUnauthorized},
		{"别的租户", path(testTask), f.ingress.IssueToken("t2", "u2", time.Hour), http.StatusNotFound},
		{"不存在的 Task", path("tsk_01J" + strings.Repeat("0", 22) + "X"), f.ingress.IssueToken("t1", "u1", time.Hour), http.StatusNotFound},
		{"格式不对的 task_id", path("not-a-task-id"), f.ingress.IssueToken("t1", "u1", time.Hour), http.StatusBadRequest},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s := f.open(c.path, c.token, nil)
			if s.resp.StatusCode != c.status {
				t.Errorf("应返回 %d，实际 %d", c.status, s.resp.StatusCode)
			}
		})
	}
}

func TestEventNamesAndURL(t *testing.T) {
	if got := EventName(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED); got != "task_finished" {
		t.Errorf("EventName 应为 task_finished，实际 %q", got)
	}
	if got := EventName(taskapiv1.TaskEventType_TASK_EVENT_TYPE_TOKEN_DELTA); got != "token_delta" {
		t.Errorf("EventName 应为 token_delta，实际 %q", got)
	}
	if got := URL("https://api.example.test/", testTask); got != "https://api.example.test/v1/tasks/"+testTask+"/events" {
		t.Errorf("订阅地址不对：%q", got)
	}
}

func TestAppendAssignsIDsAndDecodeRestoresThem(t *testing.T) {
	c, err := memcache.New(memcache.Options{Clock: testkit.NewFakeClock()}).Bind(tenant("t1"))
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	for want := int64(1); want <= 2; want++ {
		id, err := Append(ctx, c, &taskapiv1.TaskEvent{EventId: 99, TaskId: testTask, Type: taskapiv1.TaskEventType_TASK_EVENT_TYPE_VERDICT}, time.Hour)
		if err != nil || id != want {
			t.Fatalf("event_id 由流分配，从 1 递增：期望 %d，实际 %d（%v）", want, id, err)
		}
	}
	page, err := c.ReadEvents(ctx, StreamName(testTask), 0, 10)
	if err != nil || len(page.Events) != 2 {
		t.Fatalf("流里应有 2 个事件：%v %v", page, err)
	}
	ev, err := Decode(page.Events[1])
	if err != nil || ev.GetEventId() != 2 || ev.GetTaskId() != testTask {
		t.Errorf("Decode 应还原事件并取流里的 id，实际 %v（%v）", ev, err)
	}
}
