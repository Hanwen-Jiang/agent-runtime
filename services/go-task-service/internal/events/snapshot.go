package events

// 会话快照（docs/plan/feedback-2026-11-01.md N-19；人工验收基线 Q57、Q58；schemas/session_snapshot.schema.json）。
//
// GET /v1/sessions/{session_id}/snapshot 给出会话里最近的 Task、各自最近的事件（与 SSE 的 data 相同）、最新的
// plan_updated 与续传游标 last_event_id。客户端先用快照画首屏，再对没结束的 Task 用游标作 Last-Event-ID 订阅 SSE，
// 只追加增量；游标之后的事件 event_id 都比它大，不丢也不重。快照只读：不写 Cache、不建表、不起后台任务。
//
// 读不出事件流时（Cache 出错或流已丢失而业务库记着更晚的 event_id）这个 Task 退回业务库的记录（source=record：
// 终态与业务库的游标，结束的 Task 补发 task_finished），并在 errors 里报告；不悄悄给空（Q58）。解不开的一条事件跳过，
// 报告 event_undecodable，游标照样往后。

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/ids"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

const (
	routeSnapshot     = "session_snapshot"
	defaultTasks      = 20
	maxTasks          = 50
	defaultEvents     = 50
	maxEvents         = 200
	snapshotPageSize  = 500
	errEventsMissing  = "events_unavailable"
	errEventUndecoded = "event_undecodable"
)

// SessionSnapshot 是快照的响应（schemas/session_snapshot.schema.json）。
type SessionSnapshot struct {
	SessionID      string          `json:"session_id"`
	GeneratedAt    string          `json:"generated_at"`
	Tasks          []TaskSnapshot  `json:"tasks"`
	TasksTruncated bool            `json:"tasks_truncated,omitempty"`
	Errors         []SnapshotError `json:"errors"`
}

// TaskSnapshot 是快照里的一个 Task。
type TaskSnapshot struct {
	TaskID          string            `json:"task_id"`
	Status          string            `json:"status"`
	CreatedAt       string            `json:"created_at,omitempty"`
	LastEventID     int64             `json:"last_event_id"`
	Events          []json.RawMessage `json:"events"`
	EventsTruncated bool              `json:"events_truncated,omitempty"`
	Plan            json.RawMessage   `json:"plan,omitempty"`
	Source          string            `json:"source"`
}

// SnapshotError 是生成快照时一个 Task 的问题。
type SnapshotError struct {
	TaskID string `json:"task_id"`
	Code   string `json:"code"`
}

func (h *handler) snapshot(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	id, err := h.d.Ingress.Authenticate(ctx, ingress.Credentials{BearerToken: BearerToken(r.Header.Get("Authorization"))})
	if err != nil {
		http.Error(w, "unauthenticated", http.StatusUnauthorized)
		return
	}
	if err := h.d.Ingress.Admit(ctx, id, routeSnapshot); err != nil {
		admitError(w, err)
		return
	}
	sessionID := r.PathValue("session_id")
	if !ids.ValidSessionID(sessionID) {
		http.Error(w, "invalid session_id", http.StatusBadRequest)
		return
	}
	taskLimit, ok1 := limitParam(r, "tasks", defaultTasks, maxTasks)
	eventLimit, ok2 := limitParam(r, "events", defaultEvents, maxEvents)
	if !ok1 || !ok2 {
		http.Error(w, "invalid limit", http.StatusBadRequest)
		return
	}
	tenant, err := ingress.TenantContextFor(id, "", "", "", nil)
	if err != nil {
		http.Error(w, "permission denied", http.StatusForbidden)
		return
	}
	if found, err := h.tasks.SessionExists(ctx, tenant, sessionID); err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	} else if !found {
		http.Error(w, "session not found", http.StatusNotFound)
		return
	}
	rows, more, err := h.tasks.SessionTasks(ctx, tenant, sessionID, taskLimit)
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	c, cacheErr := h.d.Cache.Bind(tenant)
	out := SessionSnapshot{SessionID: sessionID, GeneratedAt: h.d.Clock.Now().UTC().Format(time.RFC3339Nano), Tasks: []TaskSnapshot{}, Errors: []SnapshotError{}, TasksTruncated: more}
	for _, row := range rows {
		var entry TaskSnapshot
		var problems []string
		// HIST-01：业务库里记着这个 Task 的事件时从它读（完整，不受 Cache 重启与保留期影响）。
		persisted, err := h.tasks.HasCompleteEvents(ctx, tenant, row.TaskID)
		switch {
		case err == nil && persisted:
			entry, problems = h.storedSnapshot(ctx, tenant, row, eventLimit)
		case cacheErr != nil:
			entry, problems = fromRecord(row), []string{errEventsMissing}
		default:
			entry, problems = taskSnapshot(ctx, c, row, eventLimit)
		}
		out.Tasks = append(out.Tasks, entry)
		for _, code := range problems {
			out.Errors = append(out.Errors, SnapshotError{TaskID: row.TaskID, Code: code})
		}
	}
	data, err := json.Marshal(out)
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(data)
}

// collected 把一个 Task 的事件按顺序收起来：留最近 limit 条、最新的 plan_updated，同一个 event_id 与第二个 task_finished 不重复收。
type collected struct {
	limit                       int
	recent                      []*taskapiv1.TaskEvent
	plan                        *taskapiv1.TaskEvent
	last                        int64
	truncated, finished, active bool
}

func (k *collected) add(ev *taskapiv1.TaskEvent) {
	if ev.GetEventId() != 0 && ev.GetEventId() <= k.last {
		return
	}
	if ev.GetType() == taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED {
		if k.finished {
			return
		}
		k.finished = true
	}
	if ev.GetType() != taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED {
		k.active = true
	}
	if ev.GetType() == taskapiv1.TaskEventType_TASK_EVENT_TYPE_PLAN_UPDATED {
		k.plan = ev
	}
	k.last = max(k.last, ev.GetEventId())
	k.recent = append(k.recent, ev)
	if len(k.recent) > k.limit {
		k.recent, k.truncated = k.recent[1:], true
	}
}

// entry 补上业务库里的结局（结束了而事件里没有 task_finished 时），给出快照里的这一项。
func (k *collected) entry(row taskstore.Task) TaskSnapshot {
	if row.Finished() && !k.finished {
		if ev, err := Finished(row); err == nil {
			k.add(ev)
		}
	}
	entry := TaskSnapshot{
		TaskID:          row.TaskID,
		Status:          statusOf(row, k.active),
		CreatedAt:       createdAt(row),
		LastEventID:     k.last,
		Events:          make([]json.RawMessage, 0, len(k.recent)),
		EventsTruncated: k.truncated,
		Source:          "events",
	}
	for _, ev := range k.recent {
		if data, err := marshal.Marshal(ev); err == nil {
			entry.Events = append(entry.Events, data)
		}
	}
	if k.plan != nil {
		if data, err := marshal.Marshal(k.plan); err == nil {
			entry.Plan = data
		}
	}
	return entry
}

// storedSnapshot 从业务库读这个 Task 的事件（HIST-01）。读不出来时退回业务库的记录并报告。
func (h *handler) storedSnapshot(ctx context.Context, tenant *commonv1.TenantContext, row taskstore.Task, limit int) (TaskSnapshot, []string) {
	k := &collected{limit: limit}
	if err := h.stored(ctx, tenant, row.TaskID, 0, func(ev *taskapiv1.TaskEvent) bool {
		k.add(ev)
		return true
	}); err != nil {
		return fromRecord(row), []string{errEventsMissing}
	}
	return k.entry(row), nil
}

// taskSnapshot 读这个 Task 的事件流，留最近 limit 条与最新的 plan_updated。
func taskSnapshot(ctx context.Context, c cache.Cache, row taskstore.Task, limit int) (TaskSnapshot, []string) {
	k := &collected{limit: limit}
	var problems []string
	var cursor int64
	for {
		page, err := c.ReadEvents(ctx, StreamName(row.TaskID), cursor, snapshotPageSize)
		if err != nil {
			return fromRecord(row), []string{errEventsMissing}
		}
		for _, se := range page.Events {
			cursor = se.ID
			ev, err := Decode(se)
			if err != nil {
				if len(problems) == 0 {
					problems = append(problems, errEventUndecoded)
				}
				continue
			}
			k.add(ev)
		}
		if len(page.Events) < snapshotPageSize {
			break
		}
	}
	// 流是空的而业务库记着分配过 event_id（task_accepted 总是第一条）：流丢了（Cache 重启、过了保留期），退回记录。
	// 不拿业务库的计数与流里最后一条比：没结束的 Task 正在追加的那条会让两者短暂不一致。
	if len(k.recent) == 0 && k.last == 0 && row.LastEventID > 0 {
		return fromRecord(row), append(problems, errEventsMissing)
	}
	return k.entry(row), problems
}

// fromRecord 是事件流读不出来时的退路：业务库的终态与游标；结束的 Task 补发 task_finished。
func fromRecord(row taskstore.Task) TaskSnapshot {
	// 结束的 Task 游标取业务库的最后一个 event_id（task_finished）；没结束的为 0：SSE 从头补上流里还留着的，之后的照常推。
	entry := TaskSnapshot{TaskID: row.TaskID, Status: statusOf(row, row.LastEventID > 1), CreatedAt: createdAt(row), Events: []json.RawMessage{}, Source: "record"}
	if row.Finished() {
		entry.LastEventID = row.LastEventID
		if ev, err := Finished(row); err == nil {
			if data, err := marshal.Marshal(ev); err == nil {
				entry.Events = append(entry.Events, data)
			}
		}
	}
	return entry
}

func statusOf(row taskstore.Task, active bool) string {
	if row.Finished() {
		return strings.ToLower(strings.TrimPrefix(row.Status.String(), "TASK_STATUS_"))
	}
	if active {
		return "running"
	}
	return "queued"
}

func createdAt(row taskstore.Task) string {
	if row.CreatedAt.IsZero() {
		return ""
	}
	return row.CreatedAt.UTC().Format(time.RFC3339Nano)
}

func limitParam(r *http.Request, name string, def, most int) (int, bool) {
	raw := r.URL.Query().Get(name)
	if raw == "" {
		return def, true
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 1 {
		return 0, false
	}
	return min(n, most), true
}

// needsResnapshot：续传的 Last-Event-ID 早于事件流还留着的最早一条（中间的已过保留期，或流丢了而业务库记着更晚的），
// 只追加增量会丢事件，要客户端重新取快照（基线清单 expired_cursor_requests_snapshot）。读不出来时不拦，照旧订阅。
func needsResnapshot(ctx context.Context, c cache.Cache, row taskstore.Task, after int64) bool {
	if after <= 0 {
		return false
	}
	page, err := c.ReadEvents(ctx, StreamName(row.TaskID), 0, 1)
	if err != nil {
		return false
	}
	if len(page.Events) == 0 {
		return row.LastEventID > after
	}
	ev, err := Decode(page.Events[0])
	return err == nil && ev.GetEventId() > after+1
}
