package events

// 逐字增量（DELTA，docs/plan/all-plans-dag-2026-11-03.md 7.1）：token_delta、thinking_delta 不分配 event_id，
// 不进业务库与 Task 的事件流，只进 Cache 里这个 Task 单独的增量流，最多留 DeltaRetention。
// SSE 只把连上之后来的增量推给在线的连接（没有 id 行），断线重连不补发；节点结束时的持久事件带完整结果。

import (
	"context"
	"strconv"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
)

// DeltaRetention 是增量在 Cache 里的保留时间（7.1：不超过 10 分钟）。
const DeltaRetention = 10 * time.Minute

// DeltaStreamName 是 Task 增量流在 Cache 里的名字。
func DeltaStreamName(taskID string) string { return "task-deltas:" + taskID }

// deltaTailKey 记着增量流最后一条的 id：SSE 新连接从它之后订阅，不补发连上之前的。
func deltaTailKey(taskID string) string { return "task-deltas-tail:" + taskID }

// IsDelta 判断事件类型是不是逐字增量。
func IsDelta(t taskapiv1.TaskEventType) bool {
	return t == taskapiv1.TaskEventType_TASK_EVENT_TYPE_TOKEN_DELTA || t == taskapiv1.TaskEventType_TASK_EVENT_TYPE_THINKING_DELTA
}

// AppendDelta 把一条增量追加到 Task 的增量流并记下流尾，返回流里的 id。
func AppendDelta(ctx context.Context, c cache.Cache, ev *taskapiv1.TaskEvent) (int64, error) {
	data, err := marshal.Marshal(ev)
	if err != nil {
		return 0, err
	}
	id, err := c.AppendEvent(ctx, DeltaStreamName(ev.GetTaskId()), data, DeltaRetention)
	if err != nil {
		return 0, err
	}
	// 并发追加时流尾可能落后一条：新连接多收一条刚到的增量，无妨。
	if err := c.Set(ctx, deltaTailKey(ev.GetTaskId()), []byte(strconv.FormatInt(id, 10)), DeltaRetention); err != nil {
		return 0, err
	}
	return id, nil
}

// deltaTail 是增量流现在的流尾；读不到时为 0（流尾过期说明 10 分钟内没有增量，流里也不剩了）。
func deltaTail(ctx context.Context, c cache.Cache, taskID string) int64 {
	raw, ok, err := c.Get(ctx, deltaTailKey(taskID))
	if err != nil || !ok {
		return 0
	}
	n, err := strconv.ParseInt(string(raw), 10, 64)
	if err != nil || n < 0 {
		return 0
	}
	return n
}
