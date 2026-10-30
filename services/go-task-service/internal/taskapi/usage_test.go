package taskapi

// TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.11 节问题 1：QueryTask 的 usage。
//
// 美元的用量按 task_state.schema.json 的口径，是各 Round 的 cost_usd 之和（Worker 在查询结果里给正在跑的 Round 带上已花的钱）。
// 从工作流的 Task State 算；查不到 Task State 时取业务库里记下的结局（TaskWorkflowResult.usage）；两样都没有时不设 usage。
// token 数这一期不填。

import (
	"context"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
)

func runningWithRounds(t *testing.T, f *apiFixture, taskID string, status durable.Status, rounds []any) {
	t.Helper()
	state, err := structpb.NewValue(map[string]any{"task_id": taskID, "goal": "g", "done_when": "d", "rounds": rounds})
	if err != nil {
		t.Fatal(err)
	}
	taskStatus := commonv1.TaskStatus_TASK_STATUS_RUNNING
	if status == durable.StatusCompleted {
		taskStatus = commonv1.TaskStatus_TASK_STATUS_DONE
	}
	f.durable.addWorkflow(taskID, "t1", session.TaskWorkflowName, status, map[string]proto.Message{
		"task_state": &workflowv1.TaskStateQueryResult{
			TaskId: taskID, Status: taskStatus, RoundSeq: int32(len(rounds)),
			TaskState: &commonv1.JsonPayload{SchemaId: "https://agentruntime.example/schemas/task_state.schema.json", Value: state},
		},
	})
}

func queryTask(t *testing.T, f *apiFixture, tok, taskID string) *taskapiv1.QueryTaskResponse {
	t.Helper()
	resp, err := f.client.QueryTask(context.Background(), authed(tok, &taskapiv1.QueryTaskRequest{TaskId: taskID}))
	if err != nil {
		t.Fatal(err)
	}
	return resp.Msg
}

func TestQueryTaskSumsTheRoundCostsIntoUsage(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	sub := f.mustSubmit(tok, newRequest("c1"))
	runningWithRounds(t, f, sub.GetTaskId(), durable.StatusRunning, []any{
		map[string]any{"seq": 1, "stage_goal": "读", "status": "completed", "cost_usd": 0.25},
		map[string]any{"seq": 2, "stage_goal": "改", "status": "completed", "cost_usd": 0.4},
		// 正在跑的 Round：Worker 的查询结果里带上已花的钱（规划与已完成的节点）。
		map[string]any{"seq": 3, "stage_goal": "测", "status": "running", "cost_usd": 0.1},
	})
	got := queryTask(t, f, tok, sub.GetTaskId())
	if got.GetUsage() == nil || got.GetUsage().GetCostUsdMicros() != 750_000 {
		t.Errorf("usage.cost_usd_micros 是各 Round 的 cost_usd 之和（0.75 美元）：%v", got.GetUsage())
	}
	if got.GetUsage().GetInputTokens() != 0 || got.GetUsage().GetOutputTokens() != 0 {
		t.Errorf("token 数这一期不填：%v", got.GetUsage())
	}
}

func TestQueryTaskCountsRoundsWithoutACostAsZero(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	sub := f.mustSubmit(tok, newRequest("c1"))
	runningWithRounds(t, f, sub.GetTaskId(), durable.StatusRunning, []any{
		map[string]any{"seq": 1, "stage_goal": "读", "status": "running"},
	})
	got := queryTask(t, f, tok, sub.GetTaskId())
	if got.GetUsage() == nil || got.GetUsage().GetCostUsdMicros() != 0 {
		t.Errorf("有 Task State 就给 usage，还没有花费的 Round 记 0：%v", got.GetUsage())
	}
}

func TestQueryFinishedTaskReportsTheUsageFromItsTaskState(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	sub := f.mustSubmit(tok, newRequest("c1"))
	runningWithRounds(t, f, sub.GetTaskId(), durable.StatusCompleted, []any{
		map[string]any{"seq": 1, "stage_goal": "读", "status": "completed", "cost_usd": 1.2345678},
	})
	// 被取消的 Task 的结局由 Session 工作流拼出、不带 usage；所以有 Task State 时以它为准，不取结局里的。
	acts := &session.Activities{Cache: memcache.New(memcache.Options{Clock: f.clock}), Store: f.store, Clock: f.clock, Retention: time.Hour}
	tenant := &commonv1.TenantContext{TenantId: "t1", UserId: "u1", SessionId: sub.GetSessionId()}
	if err := acts.FinishTask(context.Background(), tenant, &workflowv1.TaskWorkflowResult{
		TaskId: sub.GetTaskId(), Status: commonv1.TaskStatus_TASK_STATUS_DONE, Usage: &commonv1.Usage{CostUsdMicros: 1},
	}); err != nil {
		t.Fatal(err)
	}
	got := queryTask(t, f, tok, sub.GetTaskId())
	if got.GetStatus() != commonv1.TaskStatus_TASK_STATUS_DONE || got.GetUsage().GetCostUsdMicros() != 1_234_568 {
		t.Errorf("已结束的 Task 也从 Task State 算（四舍五入到微美元）：%v %v", got.GetStatus(), got.GetUsage())
	}
}

func TestQueryFinishedTaskWithoutTaskStateUsesTheRecordedUsage(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	sub := f.mustSubmit(tok, newRequest("c1"))
	// 没有子工作流可查（例如保留期过了）：只剩 FinishTask 记下的结局。
	acts := &session.Activities{Cache: memcache.New(memcache.Options{Clock: f.clock}), Store: f.store, Clock: f.clock, Retention: time.Hour}
	tenant := &commonv1.TenantContext{TenantId: "t1", UserId: "u1", SessionId: sub.GetSessionId()}
	if err := acts.FinishTask(context.Background(), tenant, &workflowv1.TaskWorkflowResult{
		TaskId: sub.GetTaskId(), Status: commonv1.TaskStatus_TASK_STATUS_ABORTED, Usage: &commonv1.Usage{CostUsdMicros: 2_810_000},
	}); err != nil {
		t.Fatal(err)
	}
	got := queryTask(t, f, tok, sub.GetTaskId())
	if got.GetStatus() != commonv1.TaskStatus_TASK_STATUS_ABORTED || got.GetUsage().GetCostUsdMicros() != 2_810_000 {
		t.Errorf("查不到 Task State 时取记下的结局里的 usage：%v %v", got.GetStatus(), got.GetUsage())
	}
}

func TestQueryTaskWithoutStateOrOutcomeLeavesUsageUnset(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	sub := f.mustSubmit(tok, newRequest("c1"))
	got := queryTask(t, f, tok, sub.GetTaskId())
	if got.GetStatus() != commonv1.TaskStatus_TASK_STATUS_QUEUED || got.Usage != nil {
		t.Errorf("排队中的 Task 没有 Task State 也没有结局：不设 usage，实际 %v", got.GetUsage())
	}
}
