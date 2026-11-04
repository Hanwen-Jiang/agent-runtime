package session

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"time"

	"go.temporal.io/sdk/temporal"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
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
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/sandboxgrants"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

const defaultRetention = 24 * time.Hour

// Activities 是 Session 工作流的 activity：把 Task 事件推入 Cache（SSE 从这里读），并把结局记进业务库。
// PublishTaskEvent 由 Python 的 Task 工作流跨语言调用，推送进度事件（T-11b）。
// 每次调用都按参数里的租户绑定 Provider。activity 可能被重试，两个方法都必须幂等。
type Activities struct {
	Cache cache.Provider
	Store relational.Provider
	Clock clock.Clock
	// Retention 是推流事件的保留时间，断线续传靠它（设计书 4.14）。
	Retention time.Duration
	// Egress 在 Task 结束、收回联网授权后通知出网网关立即断开（D-16）；为空时只靠网关几秒的缓存。
	Egress *sandboxgrants.Notifier
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
// 推送之前先收回这个 Task 的联网授权（D-16：授权属于 Task，Task 结束即收回；收回是幂等的，重试时再做一遍也无妨）。
func (a *Activities) FinishTask(ctx context.Context, tenant *commonv1.TenantContext, result *workflowv1.TaskWorkflowResult) error {
	data, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(result)
	if err != nil {
		return activityError(err)
	}
	if n, err := sandboxgrants.RevokeTask(ctx, a.Store, tenant, result.GetTaskId(), a.now()); err != nil {
		return activityError(err)
	} else if n > 0 {
		_ = a.Egress.Revoked(ctx, tenant.GetTenantId(), result.GetTaskId())
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

// publishOnce 每个 Task 的每类事件只推一次（去重键是事件类型），见 publish。
func (a *Activities) publishOnce(ctx context.Context, tenant *commonv1.TenantContext, ev *taskapiv1.TaskEvent) (int64, error) {
	id, err := a.publish(ctx, tenant, events.EventName(ev.GetType()), ev)
	ev.EventId = id
	return id, err
}

// ephemeral 是只走推流、不进业务库的事件：逐字的增量量大，历史里有节点与 Task 的结果就够了。
var ephemeral = map[taskapiv1.TaskEventType]bool{
	taskapiv1.TaskEventType_TASK_EVENT_TYPE_TOKEN_DELTA:    true,
	taskapiv1.TaskEventType_TASK_EVENT_TYPE_THINKING_DELTA: true,
}

// publish 分配 event_id 并推送，同一个去重键只推一次，返回它的 event_id：
//   - event_id 由业务库分配（taskstore.NextEventID，Cache 重启也不回退）；
//   - 先把事件记进业务库（HIST-01，taskstore.RecordEvent），再追加到 Cache 的推流，最后在 Cache 里记下标记；
//   - 重试时先看 Cache 的标记，再看业务库：记过的沿用原来的 event_id 与内容（Cache 重启丢了标记也不再分配新的），
//     只是推流里可能重复一次，SSE 端点按 event_id 去掉。逐字的增量不进业务库，去重只靠 Cache 的标记。
func (a *Activities) publish(ctx context.Context, tenant *commonv1.TenantContext, dedupe string, ev *taskapiv1.TaskEvent) (int64, error) {
	c, err := a.Cache.Bind(tenant)
	if err != nil {
		return 0, err
	}
	marker := "task_event:" + ev.GetTaskId() + ":" + dedupe
	if raw, ok, err := c.Get(ctx, marker); err != nil {
		return 0, err
	} else if ok {
		id, _ := strconv.ParseInt(string(raw), 10, 64)
		return id, nil
	}
	tasks := taskstore.New(a.Store)
	out := proto.Clone(ev).(*taskapiv1.TaskEvent)
	if out.GetSessionId() == "" {
		out.SessionId = tenant.GetSessionId()
	}
	prior, recorded := taskstore.Event{}, false
	if !ephemeral[out.GetType()] {
		if prior, recorded, err = tasks.EventByKey(ctx, tenant, out.GetTaskId(), dedupe); err != nil {
			return 0, err
		}
	}
	if recorded {
		if err := protojson.Unmarshal([]byte(prior.Data), out); err != nil {
			return 0, err
		}
		out.EventId = prior.EventID
	} else {
		if out.EventId, err = tasks.NextEventID(ctx, tenant, out.GetTaskId()); err != nil {
			return 0, err
		}
		if !ephemeral[out.GetType()] {
			data, err := events.Encode(out)
			if err != nil {
				return 0, err
			}
			stored, err := tasks.RecordEvent(ctx, tenant, taskstore.Event{
				TaskID: out.GetTaskId(), EventID: out.GetEventId(), DedupeKey: dedupe, SessionID: out.GetSessionId(),
				Type: events.EventName(out.GetType()), Data: string(data), CreatedAt: a.now(),
			})
			if err != nil {
				return 0, err
			}
			out.EventId = stored.EventID // 并发的重试先记下了：用它的
		}
	}
	if _, err := events.Append(ctx, c, out, a.retention()); err != nil {
		return 0, err
	}
	if err := c.Set(ctx, marker, []byte(strconv.FormatInt(out.GetEventId(), 10)), a.retention()); err != nil {
		return 0, err
	}
	return out.GetEventId(), nil
}

// RecordQueuedEdit 记下修改排队中的消息的结果：改好了就同步业务库的 message，并推 task_message_edited（不带文字）。
// docs/plan/running-task-interaction.md 第 2 节。一个 Task 可以改好几次，publishOnce 按事件类型去重不合用：每次修改一个去重键
// （信号的时间与消息的哈希），活动重试不重复推。没有这个 Task 时没有可告诉的人。
func (a *Activities) RecordQueuedEdit(ctx context.Context, tenant *commonv1.TenantContext, sig *workflowv1.EditQueuedTaskSignal, applied bool, reason string) error {
	tasks := taskstore.New(a.Store)
	_, ok, err := tasks.LookupTask(ctx, tenant, sig.GetTaskId())
	if err != nil {
		return activityError(err)
	}
	if !ok {
		return nil
	}
	state := "rejected"
	fields := map[string]any{"event": "task_message_edited"}
	if applied {
		state = "applied"
		if err := tasks.UpdateMessage(ctx, tenant, sig.GetTaskId(), sig.GetMessage()); err != nil {
			return activityError(err)
		}
	} else {
		fields["reason_code"] = reason
	}
	fields["state"] = state
	value, err := structpb.NewValue(fields)
	if err != nil {
		return activityError(err)
	}
	ev := &taskapiv1.TaskEvent{
		TaskId:    sig.GetTaskId(),
		SessionId: tenant.GetSessionId(),
		Type:      taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_MESSAGE_EDITED,
		EmittedAt: timestamppb.New(a.now()),
		Payload:   &commonv1.JsonPayload{Value: value},
	}
	digest := sha256.Sum256([]byte(sig.GetMessage()))
	key := "queued_edit:" + strconv.FormatInt(sig.GetAt().AsTime().UnixNano(), 10) + ":" + state + ":" + hex.EncodeToString(digest[:8])
	_, err = a.publishKeyed(ctx, tenant, key, ev)
	return err
}

// sessionOnlyEvents 只能由 Session 自己推送：受理、结束与 SSE 的心跳。
var sessionOnlyEvents = map[taskapiv1.TaskEventType]bool{
	taskapiv1.TaskEventType_TASK_EVENT_TYPE_UNSPECIFIED:   true,
	taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_ACCEPTED: true,
	taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_FINISHED: true,
	taskapiv1.TaskEventType_TASK_EVENT_TYPE_HEARTBEAT:     true,
	// 排队中的修改由 Session 工作流处理（RecordQueuedEdit）。
	taskapiv1.TaskEventType_TASK_EVENT_TYPE_TASK_MESSAGE_EDITED: true,
}

// PublishTaskEvent 推送 Task 工作流交来的一个进度事件（跨语言 activity，名字见 names.proto 的 ActivityName；T-11b），
// 返回分配的 event_id：
//   - event_id 由业务库分配，与 task_accepted、task_finished 同一个序列；session_id 取租户上下文，emitted_at 用事件里的；
//   - 同一个去重键只推一次（activity 重试），返回第一次的 event_id；去重的标记在 Cache 里，Cache 重启后可能重复一次；
//   - Task 已经结束（业务库记下了结局）时丢弃，返回 0：task_finished 永远是最后一个事件；
//   - 只能由 Session 推送的类型、缺少 task_id 或去重键、别的租户的 Task：不可重试的错误。
func (a *Activities) PublishTaskEvent(ctx context.Context, tenant *commonv1.TenantContext, key string, ev *taskapiv1.TaskEvent) (int64, error) {
	switch {
	case ev == nil || ev.GetTaskId() == "" || key == "":
		return 0, activityError(providererr.New(providererr.Invalid, "invalid_event", "进度事件要有 task_id 与去重键"))
	case sessionOnlyEvents[ev.GetType()]:
		return 0, activityError(providererr.New(providererr.Invalid, "reserved_event_type", "这类事件只能由 Session 推送："+ev.GetType().String()))
	}
	tasks := taskstore.New(a.Store)
	row, ok, err := tasks.LookupTask(ctx, tenant, ev.GetTaskId())
	if err != nil {
		return 0, activityError(err)
	}
	if !ok {
		return 0, activityError(providererr.New(providererr.NotFound, "task_not_found", "没有这个 Task 的记录"))
	}
	if row.Finished() {
		return 0, nil
	}
	return a.publishKeyed(ctx, tenant, key, ev)
}

// publishKeyed 分配 event_id 并推送；同一个去重键只推一次，返回第一次的 event_id（见 publish）。
func (a *Activities) publishKeyed(ctx context.Context, tenant *commonv1.TenantContext, key string, ev *taskapiv1.TaskEvent) (int64, error) {
	id, err := a.publish(ctx, tenant, "key:"+key, ev)
	return id, activityError(err)
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
