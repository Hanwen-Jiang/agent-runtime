package workspaceapi

import (
	"connectrpc.com/connect"
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"google.golang.org/protobuf/types/known/structpb"
	"strings"
	"sync"
	"testing"
	"time"
)

func asyncCreate(key string) *workspacev1.StartWorkspaceOperationRequest {
	return &workspacev1.StartWorkspaceOperationRequest{RequestId: key, Operation: &workspacev1.StartWorkspaceOperationRequest_Create{Create: &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w1"}}}
}
func TestAsyncReturnsWithoutWaitingAndRetriesAreDurable(t *testing.T) {
	var deps Deps
	h := newHarness(t, func(d *Deps) {
		if err := d.Store.Migrate(context.Background(), Migrations()); err != nil {
			t.Fatal(err)
		}
		deps = *d
	})
	h.w.status = durable.StatusRunning
	h.w.handle = func(*workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		t.Error("start waited for completion")
		return &workflowv1.WorkspaceOperationResult{}, nil
	}
	ctx := context.Background()
	first, err := h.client.StartWorkspaceOperation(ctx, authed(h, "t1", asyncCreate("stable-key")))
	if err != nil {
		t.Fatal(err)
	}
	op := first.Msg.OperationId
	if len(op) != 68 {
		t.Fatal(op)
	}
	var wg sync.WaitGroup
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r, e := h.client.StartWorkspaceOperation(ctx, authed(h, "t1", asyncCreate("stable-key")))
			if e != nil || r.Msg.OperationId != op {
				t.Errorf("retry %v %v", r, e)
			}
		}()
	}
	wg.Wait()
	if len(h.w.started) != 1 {
		t.Fatalf("duplicate side effects: %d starts", len(h.w.started))
	}
	query := &workspacev1.GetWorkspaceOperationRequest{WorkspaceId: "w1", OperationId: op}
	result, err := h.client.GetWorkspaceOperation(ctx, authed(h, "t1", query))
	if err != nil || result.Msg.Status != workspacev1.WorkspaceOperationStatus_WORKSPACE_OPERATION_STATUS_RUNNING {
		t.Fatalf("running %v %v", result, err)
	}
	changed := asyncCreate("stable-key")
	changed.GetCreate().WorkspaceId = "w2"
	if _, err = h.client.StartWorkspaceOperation(ctx, authed(h, "t1", changed)); connect.CodeOf(err) != connect.CodeAlreadyExists {
		t.Fatalf("input collision %v", err)
	}
	for _, tenant := range []string{"t2"} {
		if _, err = h.client.GetWorkspaceOperation(ctx, authed(h, tenant, query)); connect.CodeOf(err) != connect.CodeNotFound {
			t.Fatalf("tenant leak %v", err)
		}
	}
	wrong := authed(h, "t1", query)
	wrong.Header().Set("Authorization", "Bearer "+h.ing.IssueToken("t1", "u-bob", time.Hour))
	if _, err = h.client.GetWorkspaceOperation(ctx, wrong); connect.CodeOf(err) != connect.CodeNotFound {
		t.Fatalf("caller leak %v", err)
	}
	if _, err = h.client.GetWorkspaceOperation(ctx, authed(h, "t1", &workspacev1.GetWorkspaceOperationRequest{WorkspaceId: "w2", OperationId: op})); connect.CodeOf(err) != connect.CodeNotFound {
		t.Fatalf("workspace leak %v", err)
	}
	h.w.status = durable.StatusCompleted
	h.w.handle = func(*workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		v, _ := structpb.NewStruct(map[string]any{"workspace": map[string]any{"workspaceId": "w1"}, "sealed_credential": "secret"})
		return &workflowv1.WorkspaceOperationResult{Result: v}, nil
	}
	result, err = h.client.GetWorkspaceOperation(ctx, authed(h, "t1", query))
	if err != nil || result.Msg.Status != workspacev1.WorkspaceOperationStatus_WORKSPACE_OPERATION_STATUS_COMPLETED {
		t.Fatalf("completed %v %v", result, err)
	}
	if strings.Contains(result.Msg.String(), "secret") {
		t.Fatal("internal result leaked")
	}
	if _, err = h.client.StartWorkspaceOperation(ctx, authed(h, "t1", asyncCreate("stable-key"))); err != nil {
		t.Fatal(err)
	}
	if len(h.w.started) != 1 {
		t.Fatal("completed workflow restarted")
	}
	// Temporal retention can remove history; a committed admission still deduplicates.
	h.w.started = nil
	h.w.scopes = nil
	if _, err = h.client.StartWorkspaceOperation(ctx, authed(h, "t1", asyncCreate("stable-key"))); err != nil {
		t.Fatal(err)
	}
	if len(h.w.started) != 0 {
		t.Fatal("expired workflow was executed again")
	}
	// A new server and unavailable workflow history still read the persisted public result.
	deps.Durable = &worker{}
	restarted, err := New(deps)
	if err != nil {
		t.Fatal(err)
	}
	again, err := restarted.GetWorkspaceOperation(ctx, authed(h, "t1", query))
	if err != nil || again.Msg.Status != result.Msg.Status {
		t.Fatalf("restart lost result %v %v", again, err)
	}
}

func TestAsyncRejectsInvalidBeforeStarting(t *testing.T) {
	h := newHarness(t, func(d *Deps) {
		if err := d.Store.Migrate(context.Background(), Migrations()); err != nil {
			t.Fatal(err)
		}
	})
	for _, req := range []*workspacev1.StartWorkspaceOperationRequest{asyncCreate(""), {RequestId: "key"}, {RequestId: "x", Operation: &workspacev1.StartWorkspaceOperationRequest_Create{Create: &workspacev1.CreateWorkspaceRequest{WorkspaceId: "../bad"}}}} {
		if _, err := h.client.StartWorkspaceOperation(context.Background(), authed(h, "t1", req)); err == nil {
			t.Fatal("accepted invalid request")
		}
	}
	if len(h.w.started) != 0 {
		t.Fatal("invalid request started")
	}
}
