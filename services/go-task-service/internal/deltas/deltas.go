// Package deltas 是逐字增量的入口（DELTA，docs/plan/all-plans-dag-2026-11-03.md 7.1）：Worker 经 task-service 的
// 内部端口（AR_INTERNAL_ADDR）推 token_delta、thinking_delta，这里核对调用方的平台令牌与 Task，
// 追加到 Cache 里这个 Task 的增量流（events.AppendDelta），SSE 端点再推给在线的连接。
//
// 增量是可丢的：不进业务库、Temporal 历史与 trace，不分配 event_id。Task 不存在、属于别的租户、已经结束，
// 或租户关掉了逐字增量时，丢掉这一条，不报错（Worker 不重试，也不因此知道别的租户有没有这个 Task）。
package deltas

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"unicode/utf8"

	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/ids"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

// Path 是内部端口上收增量的路径。
const Path = "/internal/v1/deltas"

const (
	// MaxBatch 是一批最多的增量条数。
	MaxBatch = 200
	// MaxText 是一条增量的文字上限（字节）。
	MaxText = 8 << 10
	maxBody = MaxBatch*(MaxText+512) + 1024
)

// SecretPrefix 是调用方令牌在 SecretStore 平台作用域里的名字前缀：delta/<调用方>。
const SecretPrefix = "delta/"

// Deps 是入口的依赖。
type Deps struct {
	Cache cache.Provider
	Store relational.Provider
	Clock clock.Clock
	// Disabled 是关掉了逐字增量的租户（AR_DELTA_DISABLED_TENANTS）。
	Disabled map[string]bool
	// Tokens 返回登记的调用方令牌；每个请求都读一次，换令牌不用重启。
	Tokens func(context.Context) ([]secret.Value, error)
}

// Delta 是一条增量。
type Delta struct {
	TenantID string `json:"tenant_id"`
	TaskID   string `json:"task_id"`
	NodeID   string `json:"node_id"`
	Type     string `json:"type"`
	Offset   *int64 `json:"offset"`
	Text     string `json:"text"`
}

type batch struct {
	Deltas []Delta `json:"deltas"`
}

var types = map[string]taskapiv1.TaskEventType{
	"token_delta":    taskapiv1.TaskEventType_TASK_EVENT_TYPE_TOKEN_DELTA,
	"thinking_delta": taskapiv1.TaskEventType_TASK_EVENT_TYPE_THINKING_DELTA,
}

// New 返回 POST Path 的处理器。
func New(d Deps) (http.Handler, error) {
	if d.Cache == nil || d.Store == nil || d.Tokens == nil {
		return nil, errors.New("deltas: Cache、Store、Tokens 都要有")
	}
	if d.Clock == nil {
		d.Clock = clock.System{}
	}
	h := &handler{d: d, tasks: taskstore.New(d.Store)}
	mux := http.NewServeMux()
	mux.HandleFunc("POST "+Path, h.serve)
	return mux, nil
}

type handler struct {
	d     Deps
	tasks *taskstore.Store
}

func (h *handler) serve(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	ok, err := h.authorized(ctx, events.BearerToken(r.Header.Get("Authorization")))
	if err != nil {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	} else if !ok {
		http.Error(w, "unauthenticated", http.StatusUnauthorized)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil {
		http.Error(w, "unreadable body", http.StatusBadRequest)
		return
	}
	var b batch
	if len(raw) > maxBody {
		http.Error(w, "batch too large", http.StatusRequestEntityTooLarge)
		return
	}
	if err := json.Unmarshal(raw, &b); err != nil {
		http.Error(w, "invalid batch", http.StatusBadRequest)
		return
	}
	if err := validate(b.Deltas); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	accepted, dropped := 0, 0
	live := map[[2]string]*taskstore.Task{}
	for _, d := range b.Deltas {
		row, err := h.live(ctx, live, d.TenantID, d.TaskID)
		if err != nil {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		if row == nil {
			dropped++
			continue
		}
		if err := h.append(ctx, row, d); err != nil {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		accepted++
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = fmt.Fprintf(w, `{"accepted":%d,"dropped":%d}`, accepted, dropped)
}

// authorized 用常量时间逐个比对登记的令牌。
func (h *handler) authorized(ctx context.Context, presented string) (bool, error) {
	if presented == "" {
		return false, nil
	}
	tokens, err := h.d.Tokens(ctx)
	if err != nil {
		return false, err
	}
	ok := false
	for _, t := range tokens {
		if !t.IsZero() && subtle.ConstantTimeCompare(t.Reveal(), []byte(presented)) == 1 {
			ok = true
		}
	}
	return ok, nil
}

func validate(ds []Delta) error {
	if len(ds) == 0 || len(ds) > MaxBatch {
		return fmt.Errorf("一批要有 1 到 %d 条增量", MaxBatch)
	}
	for i, d := range ds {
		switch {
		case strings.TrimSpace(d.TenantID) == "":
			return fmt.Errorf("第 %d 条缺 tenant_id", i)
		case !ids.ValidTaskID(d.TaskID):
			return fmt.Errorf("第 %d 条的 task_id 格式不对", i)
		case strings.TrimSpace(d.NodeID) == "" || len(d.NodeID) > 128:
			return fmt.Errorf("第 %d 条的 node_id 不对", i)
		case types[d.Type] == taskapiv1.TaskEventType_TASK_EVENT_TYPE_UNSPECIFIED:
			return fmt.Errorf("第 %d 条的 type 只能是 token_delta 或 thinking_delta", i)
		case d.Offset == nil || *d.Offset < 0:
			return fmt.Errorf("第 %d 条缺 offset 或为负", i)
		case len(d.Text) > MaxText || !utf8.ValidString(d.Text):
			return fmt.Errorf("第 %d 条的文字超过 %d 字节或不是 UTF-8", i, MaxText)
		}
	}
	return nil
}

// live 返回还在跑、能收增量的 Task；不能收时为 nil。同一批里同一个 Task 只查一次业务库。
func (h *handler) live(ctx context.Context, seen map[[2]string]*taskstore.Task, tenantID, taskID string) (*taskstore.Task, error) {
	key := [2]string{tenantID, taskID}
	if row, ok := seen[key]; ok {
		return row, nil
	}
	var out *taskstore.Task
	if !h.d.Disabled[tenantID] {
		row, ok, err := h.tasks.LookupTask(ctx, &commonv1.TenantContext{TenantId: tenantID}, taskID)
		if err != nil {
			return nil, err
		}
		if ok && !row.Finished() {
			out = &row
		}
	}
	seen[key] = out
	return out, nil
}

func (h *handler) append(ctx context.Context, row *taskstore.Task, d Delta) error {
	payload, err := structpb.NewValue(map[string]any{"node_id": d.NodeID, "offset": *d.Offset, "text": d.Text})
	if err != nil {
		return err
	}
	c, err := h.d.Cache.Bind(&commonv1.TenantContext{TenantId: d.TenantID, SessionId: row.SessionID})
	if err != nil {
		return err
	}
	_, err = events.AppendDelta(ctx, c, &taskapiv1.TaskEvent{
		TaskId:    row.TaskID,
		SessionId: row.SessionID,
		Type:      types[d.Type],
		EmittedAt: timestamppb.New(h.d.Clock.Now()),
		Payload:   &commonv1.JsonPayload{Value: payload},
	})
	return err
}
