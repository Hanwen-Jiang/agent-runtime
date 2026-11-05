package workspaceapi

// WS-CREATE-RACE 验收：CreateWorkspace 在工作流起之前记下“在建”，工作流真正结束时才清掉；客户端中途断开（经 Cloudflare 的 524）不提前清。

import (
	"context"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/workspaceready"
)

func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal(what)
}

func TestCreateMarksTheWorkspaceUntilTheWorkflowFinishesEvenIfTheClientLeaves(t *testing.T) {
	caches := memcache.New(memcache.Options{})
	h := newHarness(t, func(d *Deps) { d.Cache = caches })
	release := make(chan struct{})
	h.w.handle = func(*workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		<-release
		return &workflowv1.WorkspaceOperationResult{Result: &structpb.Struct{}}, nil
	}
	c, err := caches.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	pending := func() bool {
		p, err := workspaceready.Pending(context.Background(), c, "w1")
		if err != nil {
			t.Fatal(err)
		}
		return p
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := h.client.CreateWorkspace(ctx, authed(h, "t1", &workspacev1.CreateWorkspaceRequest{TenantId: "t1", WorkspaceId: "w1"}))
		done <- err
	}()
	eventually(t, "建工作区的工作流起了之后要记下在建", pending)

	cancel() // 客户端走了（Cloudflare 100 秒就断）
	<-done
	time.Sleep(50 * time.Millisecond)
	if !pending() {
		t.Fatal("工作流还在跑，客户端断开不能清掉在建的标记")
	}

	close(release)
	eventually(t, "工作流结束之后要清掉在建的标记", func() bool { return !pending() })
}

func TestOtherOperationsDoNotMarkTheWorkspace(t *testing.T) {
	caches := memcache.New(memcache.Options{})
	h := newHarness(t, func(d *Deps) { d.Cache = caches })
	if _, err := h.client.GetDiff(context.Background(), authed(h, "t1", &workspacev1.GetDiffRequest{TenantId: "t1", WorkspaceId: "w1"})); err != nil {
		t.Fatal(err)
	}
	c, _ := caches.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if p, err := workspaceready.Pending(context.Background(), c, "w1"); err != nil || p {
		t.Fatalf("只有建工作区才记在建：%v %v", p, err)
	}
}

// failingDurable 起不了工作流（Temporal 连不上）。
type failingDurable struct{}

func (failingDurable) Bind(*commonv1.TenantContext) (durable.Engine, error) {
	return failingEngine{}, nil
}
func (failingDurable) BindPlatform(string) (durable.Engine, error) {
	return nil, providererr.New(providererr.Denied, "x", "x")
}
func (failingDurable) Close() error { return nil }

type failingEngine struct{ durable.Engine }

func (failingEngine) StartWorkflow(context.Context, string, durable.StartOptions) (durable.Run, error) {
	return durable.Run{}, providererr.New(providererr.Transient, "temporal_unavailable", "连不上 Temporal")
}

func TestAFailedStartLeavesNoMarker(t *testing.T) {
	caches := memcache.New(memcache.Options{})
	h := newHarness(t, func(d *Deps) { d.Cache = caches; d.Durable = failingDurable{} })
	if _, err := h.client.CreateWorkspace(context.Background(), authed(h, "t1", &workspacev1.CreateWorkspaceRequest{TenantId: "t1", WorkspaceId: "w1"})); err == nil {
		t.Fatal("起不了工作流要报错")
	}
	c, _ := caches.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if p, err := workspaceready.Pending(context.Background(), c, "w1"); err != nil || p {
		t.Fatalf("工作流没起来，不能留下在建的标记：%v %v", p, err)
	}
}
