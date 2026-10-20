package session

import (
	"context"
	"strconv"
	"time"

	"go.temporal.io/sdk/temporal"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

const defaultRetention = 24 * time.Hour

// Activities 是 Session 工作流的 activity：把 Task 事件推入 Cache（SSE 从这里读），并把结局记进业务库。
// 每次调用都按参数里的租户绑定 Provider。activity 可能被重试，两个方法都必须幂等。
type Activities struct {
	Cache cache.Provider
	Store relational.Provider
	Clock clock.Clock
	// Retention 是推流事件的保留时间，断线续传靠它（设计书 4.14）。
	Retention time.Duration
}

func (a *Activities) now() time.Time {
	if a.Clock == nil {
		return time.Now().UTC()
	}
	return a.Clock.Now()
}

func (a *Activities) retention() time.Duration {
	if a.Retention <= 0 {
		return defaultRetention
	}
	return a.Retention
}

// AcceptTask 推送 TASK_ACCEPTED。重复调用只推一次。
func (a *Activities) AcceptTask(ctx context.Context, tenant *commonv1.TenantContext, taskID string) error {
	ev := &taskapiv1.TaskEvent{
		TaskId:    taskID,
		SessionId: tenant.GetSessionId(),
		Type:      taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED,
		EmittedAt: timestamppb.New(a.now()),
	}
	_, err := a.publishOnce(ctx, tenant, ev)
	return activityError(err)
}

// FinishTask 推送 TASK_FINISHED（载荷是 Task 的结局），并把终态与结局记进业务库。重复调用只推一次：
// 业务库里已经记下结局时（上一次调用做完了），即使 Cache 里的标记随 Cache 重启丢了，也不再推。
func (a *Activities) FinishTask(ctx context.Context, tenant *commonv1.TenantContext, result *workflowv1.TaskWorkflowResult) error {
	data, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(result)
	if err != nil {
		return activityError(err)
	}
	tasks := taskstore.New(a.Store)
	if row, ok, err := tasks.LookupTask(ctx, tenant, result.GetTaskId()); err != nil {
		return activityError(err)
	} else if ok && row.Finished() {
		return nil
	}
	value := &structpb.Value{}
	if err := protojson.Unmarshal(data, value); err != nil {
		return activityError(err)
	}
	ev := &taskapiv1.TaskEvent{
		TaskId:    result.GetTaskId(),
		SessionId: tenant.GetSessionId(),
		Type:      taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED,
		EmittedAt: timestamppb.New(a.now()),
		Payload:   &commonv1.JsonPayload{Value: value},
	}
	id, err := a.publishOnce(ctx, tenant, ev)
	if err != nil {
		return activityError(err)
	}
	return activityError(tasks.RecordFinished(ctx, tenant, result.GetTaskId(), result.GetStatus(), id, string(data)))
}

// publishOnce 每个 Task 的每类事件只推一次：event_id 由业务库分配（taskstore.NextEventID，Cache 重启也不回退），
// 推送后在 Cache 里记下标记与 event_id，重试时看到标记就不再推。
// 推送成功、记标记之前崩溃时，重试会再推一次；宁可重复，不丢事件。
func (a *Activities) publishOnce(ctx context.Context, tenant *commonv1.TenantContext, ev *taskapiv1.TaskEvent) (int64, error) {
	c, err := a.Cache.Bind(tenant)
	if err != nil {
		return 0, err
	}
	marker := "task_event:" + ev.GetTaskId() + ":" + events.EventName(ev.GetType())
	if raw, ok, err := c.Get(ctx, marker); err != nil {
		return 0, err
	} else if ok {
		id, _ := strconv.ParseInt(string(raw), 10, 64)
		return id, nil
	}
	id, err := taskstore.New(a.Store).NextEventID(ctx, tenant, ev.GetTaskId())
	if err != nil {
		return 0, err
	}
	ev.EventId = id
	if _, err := events.Append(ctx, c, ev, a.retention()); err != nil {
		return 0, err
	}
	if err := c.Set(ctx, marker, []byte(strconv.FormatInt(id, 10)), a.retention()); err != nil {
		return 0, err
	}
	return id, nil
}

// activityError 让 Temporal 只重试 transient 错误，其余错误不重试（统一错误模型，S-02）。
func activityError(err error) error {
	if err == nil || providererr.IsRetryable(err) {
		return err
	}
	code := "error"
	if pe, ok := err.(*providererr.Error); ok {
		code = pe.Code
	}
	return temporal.NewNonRetryableApplicationError(err.Error(), code, err)
}
