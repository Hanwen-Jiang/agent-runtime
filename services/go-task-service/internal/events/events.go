// Package events 是 Task 事件的推流与 SSE 端点（设计书 4.14）：Session 工作流的 activity 把 TaskEvent
// 追加到 Cache 中该 Task 的流，SSE 端点从流里读出来推给客户端，断线后凭 Last-Event-ID 续传。
//
// event_id 由业务库分配（taskstore.NextEventID），写在事件里，从 1 开始严格递增；Cache 重启、流里的 id 从头再来，
// event_id 也不回退。Cache 只存可丢失的数据（设计书 4.5）：Task 已经结束、流却丢了时，SSE 端点用业务库里的结局
// 补发 task_finished（计划 S-07 场景 9）。事件里没有 event_id 时（旧数据）用流里的 id。
package events

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/ids"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

// 流里的事件与 SSE 的 data 都用 proto 字段名，与 /schemas 的 JSON 载荷一致。
var marshal = protojson.MarshalOptions{UseProtoNames: true}

const defaultHeartbeat = 15 * time.Second

// StreamName 是 Task 事件流在 Cache 里的名字。
func StreamName(taskID string) string { return "task:" + taskID }

// URL 是 Task 的 SSE 订阅地址。
func URL(publicBaseURL, taskID string) string {
	return strings.TrimRight(publicBaseURL, "/") + "/v1/tasks/" + taskID + "/events"
}

// EventName 是 SSE 的 event 字段：TaskEventType 去掉前缀后的小写名，例如 task_finished。
func EventName(t taskapiv1.TaskEventType) string {
	return strings.ToLower(strings.TrimPrefix(t.String(), "TASK_EVENT_TYPE_"))
}

// Append 把事件（连同 ev.EventId）追加到 Task 的流，返回流里的 id。
func Append(ctx context.Context, c cache.Cache, ev *taskapiv1.TaskEvent, retention time.Duration) (int64, error) {
	data, err := marshal.Marshal(ev)
	if err != nil {
		return 0, err
	}
	return c.AppendEvent(ctx, StreamName(ev.GetTaskId()), data, retention)
}

// Decode 把流里的一条还原成 TaskEvent；事件里没有 event_id 时取流里的 id。
func Decode(se cache.StreamEvent) (*taskapiv1.TaskEvent, error) {
	ev := &taskapiv1.TaskEvent{}
	if err := protojson.Unmarshal(se.Data, ev); err != nil {
		return nil, fmt.Errorf("events: 无法解码事件 %d：%w", se.ID, err)
	}
	if ev.EventId == 0 {
		ev.EventId = se.ID
	}
	return ev, nil
}

// Finished 用业务库里的结局（TaskWorkflowResult 的 protojson）重建 task_finished 事件，event_id 取业务库记下的最后一个。
func Finished(row taskstore.Task) (*taskapiv1.TaskEvent, error) {
	value := &structpb.Value{}
	if err := protojson.Unmarshal([]byte(row.Outcome), value); err != nil {
		return nil, fmt.Errorf("events: 业务库里 %s 的结局无法解码：%w", row.TaskID, err)
	}
	return &taskapiv1.TaskEvent{
		EventId:   row.LastEventID,
		TaskId:    row.TaskID,
		SessionId: row.SessionID,
		Type:      taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED,
		Payload:   &commonv1.JsonPayload{Value: value},
	}, nil
}

// HandlerDeps 是 SSE 端点的依赖。
type HandlerDeps struct {
	Ingress ingress.Ingress
	Cache   cache.Provider
	Store   relational.Provider
	// Heartbeat 是空闲时发注释行的间隔，防止代理断开连接；0 表示 15 秒。
	Heartbeat time.Duration
	// Clock 判断令牌是否过期；nil 时用系统时钟。
	Clock clock.Clock
}

// routeStream 是 SSE 在 Ingress.Admit 里的限流维度，与提交、取消等路由分开计数。
const routeStream = "stream_events"

// NewHandler 返回 GET /v1/tasks/{task_id}/events 的处理器。先认证并按租户限流（超限 429，带 Retry-After），
// 再确认 Task 属于该租户，然后从 Last-Event-ID（请求头或 last_event_id 查询参数）之后开始推送，推完 task_finished 后结束。
// 令牌过期时结束这条流（每次心跳与每个事件之前检查），客户端带新令牌与 Last-Event-ID 重连。
func NewHandler(d HandlerDeps) http.Handler {
	if d.Heartbeat <= 0 {
		d.Heartbeat = defaultHeartbeat
	}
	if d.Clock == nil {
		d.Clock = clock.System{}
	}
	h := &handler{d: d, tasks: taskstore.New(d.Store)}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/tasks/{task_id}/events", h.serve)
	return mux
}

type handler struct {
	d     HandlerDeps
	tasks *taskstore.Store
}

// BearerToken 取出 Authorization: Bearer 的令牌。
func BearerToken(header string) string {
	if token, ok := strings.CutPrefix(header, "Bearer "); ok {
		return strings.TrimSpace(token)
	}
	return ""
}

func (h *handler) serve(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	id, err := h.d.Ingress.Authenticate(ctx, ingress.Credentials{BearerToken: BearerToken(r.Header.Get("Authorization"))})
	if err != nil {
		http.Error(w, "unauthenticated", http.StatusUnauthorized)
		return
	}
	if err := h.d.Ingress.Admit(ctx, id, routeStream); err != nil {
		admitError(w, err)
		return
	}
	expired := func() bool { return !id.ExpiresAt.IsZero() && !h.d.Clock.Now().Before(id.ExpiresAt) }
	taskID := r.PathValue("task_id")
	if !ids.ValidTaskID(taskID) {
		http.Error(w, "invalid task_id", http.StatusBadRequest)
		return
	}
	after, err := lastEventID(r)
	if err != nil {
		http.Error(w, "invalid Last-Event-ID", http.StatusBadRequest)
		return
	}
	tenant, err := ingress.TenantContextFor(id, "", "", "", nil)
	if err != nil {
		http.Error(w, "permission denied", http.StatusForbidden)
		return
	}
	row, ok, err := h.tasks.LookupTask(ctx, tenant, taskID)
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	} else if !ok {
		http.Error(w, "task not found", http.StatusNotFound)
		return
	}
	c, err := h.d.Cache.Bind(tenant)
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	if row.Finished() {
		h.serveFinished(ctx, w, c, row, after)
		return
	}

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	// 从头订阅、按 event_id 过滤：Cache 重启后流里的 id 从头再来，与 event_id 对不上。
	sub, err := c.Subscribe(ctx, StreamName(taskID), 0)
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	defer sub.Close()

	flush := startStream(w)

	type item struct {
		ev  cache.StreamEvent
		err error
	}
	items := make(chan item)
	go func() {
		defer close(items)
		for {
			ev, err := sub.Next(ctx)
			select {
			case items <- item{ev, err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()

	ticker := time.NewTicker(h.d.Heartbeat)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if expired() {
				return
			}
			if _, err := fmt.Fprint(w, ": ping\n\n"); err != nil {
				return
			}
			flush()
		case it, ok := <-items:
			if !ok || it.err != nil || expired() {
				return
			}
			ev, err := Decode(it.ev)
			if err != nil {
				return
			}
			if ev.GetEventId() <= after {
				continue
			}
			if err := writeEvent(w, ev); err != nil {
				return
			}
			flush()
			if ev.GetType() == taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED {
				return
			}
		}
	}
}

// serveFinished 推送已结束 Task 在 Last-Event-ID 之后的事件，然后结束。Cache 里的推流丢了（或读不到）时，
// 用业务库里的结局补发 task_finished。
func (h *handler) serveFinished(ctx context.Context, w http.ResponseWriter, c cache.Cache, row taskstore.Task, after int64) {
	const pageSize = 500
	var out []*taskapiv1.TaskEvent
	finished := false
	var cursor int64
	for {
		page, err := c.ReadEvents(ctx, StreamName(row.TaskID), cursor, pageSize)
		if err != nil {
			break
		}
		for _, se := range page.Events {
			cursor = se.ID
			ev, err := Decode(se)
			if err != nil || ev.GetEventId() <= after {
				continue
			}
			if ev.GetType() == taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED {
				if finished {
					continue
				}
				finished = true
			}
			out = append(out, ev)
		}
		if len(page.Events) < pageSize {
			break
		}
	}
	if !finished && row.LastEventID > after {
		ev, err := Finished(row)
		if err != nil {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		out = append(out, ev)
	}
	flush := startStream(w)
	for _, ev := range out {
		if err := writeEvent(w, ev); err != nil {
			return
		}
	}
	flush()
}

// admitError 把 Admit 的错误写成响应：限流 429 并带 Retry-After（秒，至少 1），其他暂时性错误 503，其余 403。
func admitError(w http.ResponseWriter, err error) {
	var pe *providererr.Error
	switch {
	case errors.As(err, &pe) && pe.Category == providererr.Transient && pe.Code == "rate_limit":
		w.Header().Set("Retry-After", strconv.Itoa(max(1, int(math.Ceil(pe.RetryAfter.Seconds())))))
		http.Error(w, "rate limited", http.StatusTooManyRequests)
	case providererr.CategoryOf(err) == providererr.Transient:
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	default:
		http.Error(w, "permission denied", http.StatusForbidden)
	}
}

// startStream 写 SSE 的响应头，返回 flush。
func startStream(w http.ResponseWriter) func() {
	flusher, _ := w.(http.Flusher)
	header := w.Header()
	header.Set("Content-Type", "text/event-stream; charset=utf-8")
	header.Set("Cache-Control", "no-cache")
	header.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flush := func() {
		if flusher != nil {
			flusher.Flush()
		}
	}
	flush()
	return flush
}

func writeEvent(w http.ResponseWriter, ev *taskapiv1.TaskEvent) error {
	data, err := marshal.Marshal(ev)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", ev.GetEventId(), EventName(ev.GetType()), data)
	return err
}

var errBadEventID = errors.New("Last-Event-ID 必须是非负整数")

func lastEventID(r *http.Request) (int64, error) {
	raw := r.Header.Get("Last-Event-ID")
	if raw == "" {
		raw = r.URL.Query().Get("last_event_id")
	}
	if raw == "" {
		return 0, nil
	}
	n, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
	if err != nil || n < 0 {
		return 0, errBadEventID
	}
	return n, nil
}
