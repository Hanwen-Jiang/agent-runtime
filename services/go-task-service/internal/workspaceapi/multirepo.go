package workspaceapi

// 多仓库工作区（D-25，docs/plan/d25-multirepo-contract-baseline.md）：契约草案等人审（S-01），还没实现。
// 在实现之前 AttachRepo、DetachRepo、ListRepos 与 layout 为 MULTI 的 CreateWorkspace 一律 Unimplemented（multi_repo_unavailable），
// 应用据此不显示多仓库的入口；ROOT 的工作区照旧。

import (
	"context"

	"connectrpc.com/connect"
	"google.golang.org/genproto/googleapis/rpc/errdetails"

	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
)

const multiRepoUnavailable = "multi_repo_unavailable"

func errMultiRepo() error {
	ce := connect.NewError(connect.CodeUnimplemented, errMultiRepoText)
	if detail, err := connect.NewErrorDetail(&errdetails.ErrorInfo{Reason: multiRepoUnavailable, Domain: taskapi.ErrorDomain}); err == nil {
		ce.AddDetail(detail)
	}
	return ce
}

type multiRepoError string

func (e multiRepoError) Error() string { return string(e) }

const errMultiRepoText = multiRepoError("多仓库工作区（D-25）还没实现：契约草案等人审")

func (s *Server) AttachRepo(context.Context, *connect.Request[workspacev1.AttachRepoRequest]) (*connect.Response[workspacev1.AttachRepoResponse], error) {
	return nil, errMultiRepo()
}

func (s *Server) DetachRepo(context.Context, *connect.Request[workspacev1.DetachRepoRequest]) (*connect.Response[workspacev1.DetachRepoResponse], error) {
	return nil, errMultiRepo()
}

func (s *Server) ListRepos(context.Context, *connect.Request[workspacev1.ListReposRequest]) (*connect.Response[workspacev1.ListReposResponse], error) {
	return nil, errMultiRepo()
}
