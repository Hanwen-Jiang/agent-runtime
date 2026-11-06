package workspaceapi

import (
	"connectrpc.com/connect"
	"context"
	"errors"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	"testing"
)

func TestMultiRepoRoutesAndKeepsRootCompatible(t *testing.T) {
	h := newHarness(t)
	h.w.handle = func(*workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		return &workflowv1.WorkspaceOperationResult{Result: &structpb.Struct{}}, nil
	}
	ctx := context.Background()
	if _, err := h.client.CreateWorkspace(ctx, authed(h, "t1", &workspacev1.CreateWorkspaceRequest{WorkspaceId: "w1", Layout: workspacev1.WorkspaceLayout_WORKSPACE_LAYOUT_MULTI})); err != nil {
		t.Fatal(err)
	}
	if h.w.started[0].Args.AsMap()["layout"] != "multi" {
		t.Fatal("layout lost")
	}
	req := &workspacev1.AttachRepoRequest{WorkspaceId: "w1", Repo: &workspacev1.WorkspaceRepo{Name: "api", Path: "api", Url: "https://git.example.com/api.git", Access: workspacev1.RepoAccess_REPO_ACCESS_WRITE}, Credential: &workspacev1.GitCredential{Username: "bot", Password: "only-api"}}
	if _, err := h.client.AttachRepo(ctx, authed(h, "t1", req)); err != nil {
		t.Fatal(err)
	}
	in := h.w.started[1]
	if in.Operation != workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_ATTACH_REPO || in.SealedCredential == "" {
		t.Fatal("attach or sealing lost")
	}
	repo := in.Args.AsMap()["repo"].(map[string]any)
	if repo["name"] != "api" || repo["access"] != "write" {
		t.Fatal(repo)
	}
	for _, path := range []string{"../x", ".git", "a/b", "/tmp/x"} {
		bad := proto.Clone(req).(*workspacev1.AttachRepoRequest)
		bad.Repo.Path = path
		if _, err := h.client.AttachRepo(ctx, authed(h, "t1", bad)); err == nil {
			t.Fatal(path)
		}
	}
	if _, err := h.client.GetDiff(ctx, authed(h, "t1", &workspacev1.GetDiffRequest{WorkspaceId: "w1", Repo: "api"})); err != nil {
		t.Fatal(err)
	}
	if h.w.started[len(h.w.started)-1].Args.AsMap()["repo"] != "api" {
		t.Fatal("repo selector lost")
	}
	if _, err := h.client.DetachRepo(ctx, authed(h, "t1", &workspacev1.DetachRepoRequest{WorkspaceId: "w1", Repo: "api", DiscardChanges: true})); err != nil {
		t.Fatal(err)
	}
	if h.w.started[len(h.w.started)-1].Args.AsMap()["discard_changes"] != true {
		t.Fatal("discard flag lost")
	}
}

func reasonOf(t *testing.T, err error) (connect.Code, string) {
	t.Helper()
	var ce *connect.Error
	if !errors.As(err, &ce) {
		t.Fatalf("expected Connect error: %v", err)
	}
	for _, d := range ce.Details() {
		if v, e := d.Value(); e == nil {
			if info, ok := v.(*errdetails.ErrorInfo); ok {
				return ce.Code(), info.GetReason()
			}
		}
	}
	return ce.Code(), ""
}
