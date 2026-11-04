package workspaceapi

// D-25 的契约草案（等人审）落地之前：多仓库的几个方法与 layout 为 MULTI 的建工作区一律 Unimplemented（multi_repo_unavailable），
// 应用据此不显示多仓库的入口。

import (
	"context"
	"errors"
	"testing"

	"connectrpc.com/connect"
	"google.golang.org/genproto/googleapis/rpc/errdetails"

	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
)

func reasonOf(t *testing.T, err error) (connect.Code, string) {
	t.Helper()
	var ce *connect.Error
	if !errors.As(err, &ce) {
		t.Fatalf("应是 connect 的错误：%v", err)
	}
	for _, d := range ce.Details() {
		if v, err := d.Value(); err == nil {
			if info, ok := v.(*errdetails.ErrorInfo); ok {
				return ce.Code(), info.GetReason()
			}
		}
	}
	return ce.Code(), ""
}

func TestMultiRepoIsUnavailableUntilTheContractIsApproved(t *testing.T) {
	s := &Server{}
	ctx := context.Background()
	_, attach := s.AttachRepo(ctx, connect.NewRequest(&workspacev1.AttachRepoRequest{}))
	_, detach := s.DetachRepo(ctx, connect.NewRequest(&workspacev1.DetachRepoRequest{}))
	_, list := s.ListRepos(ctx, connect.NewRequest(&workspacev1.ListReposRequest{}))
	_, create := s.CreateWorkspace(ctx, connect.NewRequest(&workspacev1.CreateWorkspaceRequest{Layout: workspacev1.WorkspaceLayout_WORKSPACE_LAYOUT_MULTI}))
	for name, err := range map[string]error{"AttachRepo": attach, "DetachRepo": detach, "ListRepos": list, "CreateWorkspace(MULTI)": create} {
		if code, reason := reasonOf(t, err); code != connect.CodeUnimplemented || reason != multiRepoUnavailable {
			t.Errorf("%s：应是 Unimplemented / %s，实际 %v / %q", name, multiRepoUnavailable, code, reason)
		}
	}
}
