package workspaceapi

import (
	"connectrpc.com/connect"
	"context"
	"errors"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
	"google.golang.org/protobuf/types/known/timestamppb"
	"regexp"
	"time"
)

var repoName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

func (s *Server) AttachRepo(ctx context.Context, req *connect.Request[workspacev1.AttachRepoRequest]) (*connect.Response[workspacev1.AttachRepoResponse], error) {
	m := req.Msg
	r := m.GetRepo()
	if !repoName.MatchString(r.GetName()) {
		return nil, taskapi.ConnectError(invalid("invalid_repo_name", "仓库名格式不合法"))
	}
	if !repoName.MatchString(r.GetPath()) {
		return nil, taskapi.ConnectError(invalid("invalid_repo_path", "仓库路径必须是安全的单层目录名"))
	}
	if r.GetUrl() == "" {
		return nil, taskapi.ConnectError(invalid("invalid_repo_url", "要给仓库地址"))
	}
	if err := s.checkRepoURL(r.GetUrl()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	if err := checkRef("ref", r.GetRef()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	access := "read"
	switch r.GetAccess() {
	case workspacev1.RepoAccess_REPO_ACCESS_UNSPECIFIED, workspacev1.RepoAccess_REPO_ACCESS_READ:
	case workspacev1.RepoAccess_REPO_ACCESS_WRITE:
		access = "write"
	default:
		return nil, taskapi.ConnectError(invalid("invalid_repo_access", "仓库权限不合法"))
	}
	out := &workspacev1.AttachRepoResponse{}
	args := map[string]any{"repo": map[string]any{"name": r.GetName(), "url": r.GetUrl(), "ref": r.GetRef(), "path": r.GetPath(), "access": access, "primary": r.GetPrimary()}}
	if err := s.operate(ctx, req.Header(), "workspace_attach", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_ATTACH_REPO, args, m.GetCredential(), out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}
func (s *Server) DetachRepo(ctx context.Context, req *connect.Request[workspacev1.DetachRepoRequest]) (*connect.Response[workspacev1.DetachRepoResponse], error) {
	m := req.Msg
	if !repoName.MatchString(m.GetRepo()) {
		return nil, taskapi.ConnectError(invalid("invalid_repo_name", "要给仓库名"))
	}
	out := &workspacev1.DetachRepoResponse{}
	if err := s.operate(ctx, req.Header(), "workspace_detach", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_DETACH_REPO, map[string]any{"repo": m.GetRepo(), "discard_changes": m.GetDiscardChanges()}, nil, out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}
func (s *Server) ListRepos(ctx context.Context, req *connect.Request[workspacev1.ListReposRequest]) (*connect.Response[workspacev1.ListReposResponse], error) {
	m := req.Msg
	out := &workspacev1.ListReposResponse{}
	if err := s.operate(ctx, req.Header(), "workspace_repos", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LIST_REPOS, nil, nil, out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}

// Sandbox's root-only read API has no repo selector; multi reads use the existing Worker operation.
func (s *Server) multiLayout(ctx context.Context, tenant *commonv1.TenantContext, ws string) bool {
	db, err := s.d.Store.Bind(tenant)
	if err != nil {
		return false
	}
	multi := false
	_ = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, `SELECT layout FROM workspaces WHERE workspace_id = $1`, ws)
		if err == nil && ok {
			multi = row["layout"] == "multi"
		}
		return err
	})
	return multi
}

func decodeRepo(row relational.Row) *workspacev1.WorkspaceRepo {
	str := func(k string) string { v, _ := row[k].(string); return v }
	r := &workspacev1.WorkspaceRepo{Name: str("name"), Url: str("url"), Ref: str("ref"), Path: str("path"), Head: str("head"), Branch: str("branch"), Access: workspacev1.RepoAccess_REPO_ACCESS_READ}
	if str("access") == "write" {
		r.Access = workspacev1.RepoAccess_REPO_ACCESS_WRITE
	}
	r.Primary = row["is_primary"] == int64(1)
	if t, err := time.Parse(time.RFC3339Nano, str("attached_at")); err == nil {
		r.AttachedAt = timestamppb.New(t)
	}
	return r
}

func workspaceError(err error) error {
	converted := taskapi.ConnectError(err)
	var pe *providererr.Error
	if !errors.As(err, &pe) {
		return converted
	}
	var code connect.Code
	switch pe.Code {
	case "repo_dirty", "workspace_not_multi", "layout_mismatch", "primary_exists":
		code = connect.CodeFailedPrecondition
	case "max_repos", "max_bytes":
		code = connect.CodeResourceExhausted
	default:
		return converted
	}
	out := connect.NewError(code, pe)
	var ce *connect.Error
	if errors.As(converted, &ce) {
		for _, detail := range ce.Details() {
			out.AddDetail(detail)
		}
	}
	return out
}
