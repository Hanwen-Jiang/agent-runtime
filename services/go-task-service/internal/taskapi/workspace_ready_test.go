package taskapi

// WS-CREATE-RACE 验收（docs/plan/all-plans-dag-2026-11-03.md）：工作区还在建（CreateWorkspace 的工作流没做完）时不收在它上面的 Task。
// 线上冒烟 3 里，客户端经 Cloudflare 拿到 524 就提交了 Task，Task 另起一个空环境，之后的快照盖掉了建工作区时写的文件。

import (
	"context"
	"testing"
	"time"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/workspaceready"
)

func TestSubmitIsRefusedWhileTheWorkspaceIsStillBeingCreated(t *testing.T) {
	ctx := context.Background()
	caches := memcache.New(memcache.Options{})
	f := newAPIWith(t, func(d *Deps) { d.Cache = caches })
	tok := f.token("t1", "u1")
	c, err := caches.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	if err := workspaceready.Mark(ctx, c, "w1", "wso-1", time.Hour); err != nil {
		t.Fatal(err)
	}

	_, err = f.submit(tok, &taskapiv1.SubmitTaskRequest{ClientRequestId: "c1", Message: "看看工作区", WorkspaceId: "w1"})
	wantCode(t, "工作区还在建", err, connect.CodeUnavailable)
	if got := reasonOf(err); got != "workspace_not_ready" {
		t.Fatalf("reason 要是 workspace_not_ready，实际 %q", got)
	}
	if calls := f.durable.callsOf("SignalWithStart"); len(calls) != 0 {
		t.Fatalf("被拒的提交不能起工作流：%v", calls)
	}

	// 别的工作区、别的租户里同名的工作区、不带工作区的提交都不受影响
	f.mustSubmit(tok, &taskapiv1.SubmitTaskRequest{ClientRequestId: "c2", Message: "别的工作区", WorkspaceId: "w2"})
	f.mustSubmit(f.token("t2", "u2"), &taskapiv1.SubmitTaskRequest{ClientRequestId: "c3", Message: "别的租户", WorkspaceId: "w1"})
	f.mustSubmit(tok, &taskapiv1.SubmitTaskRequest{ClientRequestId: "c4", Message: "不带工作区"})

	// 建完（标记清掉）之后照常收
	if err := workspaceready.Clear(ctx, c, "w1", "wso-1"); err != nil {
		t.Fatal(err)
	}
	f.mustSubmit(tok, &taskapiv1.SubmitTaskRequest{ClientRequestId: "c5", Message: "建好了", WorkspaceId: "w1"})
}

func TestWithoutACacheSubmitDoesNotCheck(t *testing.T) {
	f := newAPI(t)
	f.mustSubmit(f.token("t1", "u1"), &taskapiv1.SubmitTaskRequest{ClientRequestId: "c1", Message: "没有 Cache", WorkspaceId: "w1"})
}

func TestClearOnlyRemovesItsOwnOperation(t *testing.T) {
	ctx := context.Background()
	c, err := memcache.New(memcache.Options{}).Bind(&commonv1.TenantContext{TenantId: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	if err := workspaceready.Mark(ctx, c, "w1", "wso-new", time.Hour); err != nil {
		t.Fatal(err)
	}
	// 一个更早的建（已经结束、标记被新的覆盖）来清，不能清掉新的
	if err := workspaceready.Clear(ctx, c, "w1", "wso-old"); err != nil {
		t.Fatal(err)
	}
	if pending, err := workspaceready.Pending(ctx, c, "w1"); err != nil || !pending {
		t.Fatalf("新的建还在做，标记要留着：%v %v", pending, err)
	}
}
