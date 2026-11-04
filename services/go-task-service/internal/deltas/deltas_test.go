package deltas

// DELTA（docs/plan/all-plans-dag-2026-11-03.md 7.1：做、默认打开）的入口：Worker 经 task-service 的内部端口推 token_delta、thinking_delta。
//
//  1. 要调用方的平台令牌（SecretStore 平台作用域，按调用方各一个）；没有或不对 401，什么都不写。
//  2. 增量只进 Cache 里这个 Task 的增量流（保留 10 分钟），不分配 event_id，不进业务库，也不进 Task 的事件流。
//  3. Task 不存在、属于别的租户、已经结束：丢掉（不报错，不透露存在与否）；租户关掉了逐字增量（AR_DELTA_DISABLED_TENANTS）同样丢掉。
//  4. 一批最多 200 条，每条的文字最多 8 KiB；type 只能是 token_delta、thinking_delta；格式不对 400，整批不写。

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

var (
	running  = "tsk_01J" + strings.Repeat("0", 20) + "RN0"
	finished = "tsk_01J" + strings.Repeat("0", 20) + "FN0"
	missing  = "tsk_01J" + strings.Repeat("0", 20) + "MS0"
	session  = "ses_01J" + strings.Repeat("0", 22) + "S"
)

const token = "worker-delta-token-0123456789abcdef"

type fixture struct {
	t     *testing.T
	cache *memcache.Provider
	store *taskstore.Store
	url   string
}

func newFixture(t *testing.T, disabled map[string]bool) *fixture {
	t.Helper()
	clk := testkit.NewFakeClock()
	c := memcache.New(memcache.Options{Clock: clk})
	db, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	ctx := context.Background()
	if err := db.Migrate(ctx, taskstore.Migrations()); err != nil {
		t.Fatal(err)
	}
	store := taskstore.New(db)
	for _, id := range []string{running, finished} {
		if _, _, err := store.InsertTask(ctx, tenantOf("t1"), taskstore.Task{TaskID: id, SessionID: session, ClientRequestID: id, CreatedAt: clk.Now()}); err != nil {
			t.Fatal(err)
		}
	}
	outcome := `{"task_id":"` + finished + `","status":"TASK_STATUS_DONE"}`
	if err := store.RecordFinished(ctx, tenantOf("t1"), finished, commonv1.TaskStatus_TASK_STATUS_DONE, 3, outcome); err != nil {
		t.Fatal(err)
	}
	h, err := New(Deps{
		Cache: c, Store: db, Clock: clk, Disabled: disabled,
		Tokens: func(context.Context) ([]secret.Value, error) { return []secret.Value{secret.New(token)}, nil },
	})
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return &fixture{t: t, cache: c, store: store, url: srv.URL}
}

func tenantOf(id string) *commonv1.TenantContext { return &commonv1.TenantContext{TenantId: id} }

func delta(tenant, task, typ, text string, offset int) map[string]any {
	return map[string]any{"tenant_id": tenant, "task_id": task, "node_id": "answer", "type": typ, "offset": offset, "text": text}
}

func (f *fixture) post(auth string, body any) (int, map[string]any) {
	f.t.Helper()
	raw, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPost, f.url+Path, bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	if auth != "" {
		req.Header.Set("Authorization", "Bearer "+auth)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		f.t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

// stored 是这个租户、这个 Task 的增量流里的事件。
func (f *fixture) stored(tenant, task string) []*taskapiv1.TaskEvent {
	f.t.Helper()
	c, err := f.cache.Bind(tenantOf(tenant))
	if err != nil {
		f.t.Fatal(err)
	}
	page, err := c.ReadEvents(context.Background(), events.DeltaStreamName(task), 0, 100)
	if err != nil {
		f.t.Fatal(err)
	}
	var out []*taskapiv1.TaskEvent
	for _, se := range page.Events {
		ev := &taskapiv1.TaskEvent{}
		if err := protojson.Unmarshal(se.Data, ev); err != nil {
			f.t.Fatal(err)
		}
		out = append(out, ev)
	}
	return out
}

func TestDeltasNeedACallerToken(t *testing.T) {
	f := newFixture(t, nil)
	batch := map[string]any{"deltas": []any{delta("t1", running, "token_delta", "Hel", 0)}}
	for _, auth := range []string{"", "wrong-token"} {
		if code, _ := f.post(auth, batch); code != http.StatusUnauthorized {
			t.Errorf("令牌 %q：应 401，实际 %d", auth, code)
		}
	}
	if got := f.stored("t1", running); len(got) != 0 {
		t.Fatalf("没认证的增量不写：%v", got)
	}
}

func TestADeltaGoesOnlyToTheDeltaStream(t *testing.T) {
	f := newFixture(t, nil)
	code, out := f.post(token, map[string]any{"deltas": []any{
		delta("t1", running, "token_delta", "Hel", 0), delta("t1", running, "thinking_delta", "想一想", 0),
	}})
	if code != http.StatusOK || out["accepted"] != float64(2) || out["dropped"] != float64(0) {
		t.Fatalf("应收下两条：%d %v", code, out)
	}
	got := f.stored("t1", running)
	if len(got) != 2 || got[0].GetType() != taskapiv1.TaskEventType_TASK_EVENT_TYPE_TOKEN_DELTA ||
		got[1].GetType() != taskapiv1.TaskEventType_TASK_EVENT_TYPE_THINKING_DELTA {
		t.Fatalf("进增量流，类型对：%v", got)
	}
	fields := got[0].GetPayload().GetValue().GetStructValue().GetFields()
	if got[0].GetEventId() != 0 || got[0].GetSessionId() != session || fields["text"].GetStringValue() != "Hel" ||
		fields["node_id"].GetStringValue() != "answer" || fields["offset"].GetNumberValue() != 0 {
		t.Fatalf("不分配 event_id，带 Session、节点、offset 与文字：%v", got[0])
	}
	c, _ := f.cache.Bind(tenantOf("t1"))
	if page, _ := c.ReadEvents(context.Background(), events.StreamName(running), 0, 10); len(page.Events) != 0 {
		t.Fatalf("不进 Task 的事件流：%v", page.Events)
	}
	if rows, err := f.store.ListEvents(context.Background(), tenantOf("t1"), running, 0, 10); err != nil || len(rows) != 0 {
		t.Fatalf("不进业务库：%v %v", rows, err)
	}
}

func TestDeltasForTasksThatCannotTakeThemAreDropped(t *testing.T) {
	f := newFixture(t, map[string]bool{"t9": true})
	code, out := f.post(token, map[string]any{"deltas": []any{
		delta("t1", finished, "token_delta", "late", 0), // 已经结束
		delta("t1", missing, "token_delta", "x", 0),     // 不存在
		delta("t2", running, "token_delta", "x", 0),     // 别的租户
		delta("t9", running, "token_delta", "x", 0),     // 关掉了逐字增量的租户
	}})
	if code != http.StatusOK || out["accepted"] != float64(0) || out["dropped"] != float64(4) {
		t.Fatalf("都丢掉，不报错：%d %v", code, out)
	}
	for _, tc := range [][2]string{{"t1", finished}, {"t2", running}, {"t9", running}} {
		if got := f.stored(tc[0], tc[1]); len(got) != 0 {
			t.Errorf("%s/%s 不该写：%v", tc[0], tc[1], got)
		}
	}
}

func TestBadBatchesAreRejectedWhole(t *testing.T) {
	f := newFixture(t, nil)
	many := make([]any, 201)
	for i := range many {
		many[i] = delta("t1", running, "token_delta", "x", i)
	}
	for what, body := range map[string]any{
		"超过 200 条":     map[string]any{"deltas": many},
		"文字超过 8 KiB":   map[string]any{"deltas": []any{delta("t1", running, "token_delta", strings.Repeat("x", 8<<10+1), 0)}},
		"type 不认识":     map[string]any{"deltas": []any{delta("t1", running, "task_finished", "x", 0)}},
		"task_id 格式不对": map[string]any{"deltas": []any{delta("t1", "nope", "token_delta", "x", 0)}},
		"offset 为负":    map[string]any{"deltas": []any{delta("t1", running, "token_delta", "x", -1)}},
		"缺 node_id":    map[string]any{"deltas": []any{map[string]any{"tenant_id": "t1", "task_id": running, "type": "token_delta", "text": "x"}}},
		"不是 JSON 对象":   "x",
		"混进一条坏的": map[string]any{"deltas": []any{
			delta("t1", running, "token_delta", "ok", 0), delta("t1", running, "nope", "x", 1),
		}},
	} {
		if code, _ := f.post(token, body); code != http.StatusBadRequest {
			t.Errorf("%s：应 400，实际 %d", what, code)
		}
	}
	if got := f.stored("t1", running); len(got) != 0 {
		t.Fatalf("格式不对的整批不写：%v", got)
	}
}

func TestOldDeltasExpire(t *testing.T) {
	if events.DeltaRetention > 10*time.Minute {
		t.Fatalf("增量最多留 10 分钟（7.1）：%v", events.DeltaRetention)
	}
}
