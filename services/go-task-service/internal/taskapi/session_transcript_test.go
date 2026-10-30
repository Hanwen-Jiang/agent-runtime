package taskapi

// TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.13 节：task-service 把每个 Task 的命令记进业务库（tasks.message），
// Worker 的 activity 据此读同一 Session 里之前的对话（设计书 3.1.1 的“追加记录”）。交付在结局（outcome）里，已经有了。

import (
	"context"
	"testing"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func TestSubmitRecordsTheCommand(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	req := newRequest("c1")
	req.Message = "读一下这个仓库讲讲结构"
	sub := f.mustSubmit(tok, req)
	row, ok, err := f.tasks.LookupTask(context.Background(), &commonv1.TenantContext{TenantId: "t1", UserId: "u1"}, sub.GetTaskId())
	if err != nil || !ok {
		t.Fatalf("应当查得到这个 Task：%v（%v）", ok, err)
	}
	if row.Message != req.Message {
		t.Errorf("tasks.message 记下命令原文：%q", row.Message)
	}
}

func TestResubmittingKeepsTheFirstCommand(t *testing.T) {
	f := newAPI(t)
	tok := f.token("t1", "u1")
	first := newRequest("c1")
	first.Message = "第一次提交"
	sub := f.mustSubmit(tok, first)
	again := newRequest("c1")
	again.Message = "同一个 client_request_id 的重试"
	if got := f.mustSubmit(tok, again); got.GetTaskId() != sub.GetTaskId() {
		t.Fatalf("重复提交返回同一个 task_id：%s、%s", got.GetTaskId(), sub.GetTaskId())
	}
	row, _, err := f.tasks.LookupTask(context.Background(), &commonv1.TenantContext{TenantId: "t1", UserId: "u1"}, sub.GetTaskId())
	if err != nil {
		t.Fatal(err)
	}
	if row.Message != "第一次提交" {
		t.Errorf("重试不改已经记下的命令：%q", row.Message)
	}
}
