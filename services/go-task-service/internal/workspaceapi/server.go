// Package workspaceapi 是 WorkspaceService 的 connect-go 实现（设计书改动稿 W2、新增 4.10.1；docs/plan/workspace-layer.md 第 5 节）：
// 与 Task API 同一个端口、同样认证。
//
// 租户由认证身份确定；请求里的 tenant_id 只用来校验一致（ingress.TenantContextFor）。别的租户的工作区一律 NotFound
// （Worker 按租户读业务库，查不到）。每个操作起一个 Python 的 WorkspaceOperationWorkflow（task 队列，ID wsop-<operation_id>）
// 并等它的结果；操作在承载工作区的沙箱里执行。只有 ListWorkspaces 直接读业务库（表由 Worker 迁移，还没有时为空）。
//
// 凭据（GitCredential）不进事件历史与日志：用 SecretStore 平台作用域的 workspace/credential-key 封成 wsc1（libs/go-core/sealing，
// 关联数据是租户与 operation_id），Worker 解开后只交给这一次的 git 命令。推送是 write_irreversible：这次调用本身是调用者的批准，
// 发起的用户作为批准人写进 gate 记录（Worker 一侧）。
package workspaceapi

import (
	"context"
	"crypto/rand"
	"encoding/base32"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/sealing"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1/workspacev1connect"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/sandboxgrants"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
)

// WorkflowName 是 Worker 一侧的工作流名，等于 names.proto 里 WORKFLOW_TYPE_WORKSPACE_OPERATION 的 wire_name（names_test.go 比对）。
const WorkflowName = "WorkspaceOperationWorkflow"

// WorkflowID 是一次操作的工作流 ID。
func WorkflowID(operationID string) string { return "wsop-" + operationID }

var (
	slug       = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
	gitRef     = regexp.MustCompile(`^[A-Za-z0-9._/@^~+-]{1,200}$`)
	opIDEncode = base32.NewEncoding("0123456789abcdefghjkmnpqrstvwxyz").WithPadding(base32.NoPadding)
)

const (
	maxURL     = 2048
	maxMessage = 10_000
)

// Deps 是 Server 的依赖。
type Deps struct {
	Ingress ingress.Ingress
	Durable durable.Provider
	Store   relational.Provider
	// SealingKey 读 SecretStore 平台作用域的 workspace/credential-key；没有时返回 nil，带凭据的请求被拒（invalid）。
	SealingKey func(ctx context.Context) ([]byte, error)
	// Timeout 是等一次操作的最长时间；超过时返回 Unavailable（操作还在跑，应用稍后看结果或重试）。默认 10 分钟。
	Timeout time.Duration
	// AllowLocalRemotes 让它收 file:// 的仓库地址：只给测试与开发（Worker 读同一个开关）。
	AllowLocalRemotes bool
	// NewOperationID 生成 operation_id；为空时用随机的 wso-<26 位>。
	NewOperationID func() string
}

// Server 实现 workspacev1connect.WorkspaceServiceHandler。ListFiles、ReadFile、GetLog、DiscardChanges、SyncWorkspace 是
// 待人审的契约草案（docs/plan/workspace-layer.md 第 7 节），审过之前返回 Unimplemented。
type Server struct {
	workspacev1connect.UnimplementedWorkspaceServiceHandler
	d Deps
}

var _ workspacev1connect.WorkspaceServiceHandler = (*Server)(nil)

// New 校验依赖并构造 Server。
func New(d Deps) (*Server, error) {
	if d.Ingress == nil || d.Durable == nil || d.Store == nil {
		return nil, errors.New("workspaceapi: Ingress、Durable、Store 都不能为空")
	}
	if d.Timeout <= 0 {
		d.Timeout = 10 * time.Minute
	}
	if d.NewOperationID == nil {
		d.NewOperationID = newOperationID
	}
	if d.SealingKey == nil {
		d.SealingKey = func(context.Context) ([]byte, error) { return nil, nil }
	}
	return &Server{d: d}, nil
}

// Mount 把 WorkspaceService 挂到 mux 上。
func (s *Server) Mount(mux *http.ServeMux, opts ...connect.HandlerOption) {
	path, handler := workspacev1connect.NewWorkspaceServiceHandler(s, opts...)
	mux.Handle(path, handler)
}

func newOperationID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return "wso-" + opIDEncode.EncodeToString(b)[:26]
}

func invalid(code, msg string) error { return providererr.New(providererr.Invalid, code, msg) }

// caller 认证、限流并按身份构造租户上下文（workspace_id 一并校验格式）。
func (s *Server) caller(ctx context.Context, header http.Header, route, requestTenant, workspaceID string) (ingress.Identity, *commonv1.TenantContext, error) {
	id, err := s.d.Ingress.Authenticate(ctx, ingress.Credentials{BearerToken: events.BearerToken(header.Get("Authorization"))})
	if err != nil {
		return id, nil, err
	}
	if err := s.d.Ingress.Admit(ctx, id, route); err != nil {
		return id, nil, err
	}
	if workspaceID != "" && !slug.MatchString(workspaceID) {
		return id, nil, invalid("invalid_workspace", "workspace_id 的格式不对（同租户 ID：小写字母、数字、_ 与 -，最多 63 个字符）")
	}
	tenant, err := ingress.TenantContextFor(id, requestTenant, workspaceID, "", nil)
	if err != nil {
		return id, nil, err
	}
	return id, tenant, nil
}

// operate 起一次工作区操作的工作流并等结果，结果按 protojson 写进 out。
func (s *Server) operate(
	ctx context.Context, header http.Header, route, requestTenant, workspaceID string, op workflowv1.WorkspaceOperation,
	args map[string]any, credential *workspacev1.GitCredential, out proto.Message,
) error {
	if workspaceID == "" {
		return invalid("invalid_workspace", "要给 workspace_id")
	}
	id, tenant, err := s.caller(ctx, header, route, requestTenant, workspaceID)
	if err != nil {
		return err
	}
	operationID := s.d.NewOperationID()
	in := &workflowv1.WorkspaceOperationInput{
		Tenant: tenant, OperationId: operationID, Operation: op, WorkspaceId: workspaceID, RequestedBy: id.UserID,
	}
	if args != nil {
		st, err := structpb.NewStruct(args)
		if err != nil {
			return invalid("invalid_argument", "参数格式不对")
		}
		in.Args = st
	}
	if credential != nil && (credential.GetUsername() != "" || credential.GetPassword() != "") {
		key, err := s.d.SealingKey(ctx)
		if err != nil {
			return err
		}
		if len(key) == 0 {
			return invalid("missing_sealing_key", "部署没有配封凭据的密钥（SecretStore 平台作用域的 "+sealing.SecretName+"），不能带凭据")
		}
		plain, _ := json.Marshal(map[string]string{"username": credential.GetUsername(), "password": credential.GetPassword()})
		if in.SealedCredential, err = sealing.Seal(key, tenant.GetTenantId(), operationID, plain, nil); err != nil {
			return err
		}
	}
	engine, err := s.d.Durable.Bind(tenant)
	if err != nil {
		return err
	}
	if _, err := engine.StartWorkflow(ctx, WorkflowName, durable.StartOptions{
		WorkflowID: WorkflowID(operationID), TaskQueue: session.TaskTaskQueue, Input: in, ExecutionTimeout: s.d.Timeout + 30*time.Minute,
	}); err != nil {
		return err
	}
	result := &workflowv1.WorkspaceOperationResult{}
	outcome, err := engine.Result(ctx, WorkflowID(operationID), result, s.d.Timeout)
	if err != nil {
		if providererr.CategoryOf(err) == providererr.Transient {
			return providererr.New(providererr.Transient, "workspace_operation_pending", "工作区的操作还没做完（"+operationID+"），稍后再试")
		}
		return err
	}
	if outcome.Status != durable.StatusCompleted {
		return providererr.New(providererr.Transient, "workspace_operation_failed", "工作区的操作没有做完，稍后再试")
	}
	if e := result.GetError(); e != nil && e.GetCategory() != commonv1.ErrorCategory_ERROR_CATEGORY_UNSPECIFIED {
		return providererr.FromProto(e)
	}
	data, err := protojson.Marshal(result.GetResult())
	if err != nil {
		return err
	}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(data, out); err != nil {
		return providererr.Wrap(providererr.Transient, "workspace_bad_result", "Worker 返回的结果格式不对", err)
	}
	return nil
}

func (s *Server) checkRepoURL(raw string) error {
	if raw == "" {
		return nil
	}
	u, err := url.Parse(raw)
	switch {
	case err != nil || len(raw) > maxURL:
		return invalid("invalid_repo_url", "仓库地址不合法")
	case u.User != nil:
		return invalid("credentials_in_url", "仓库地址里不能带凭据，凭据放 credential")
	case u.Scheme == "https" && u.Host != "":
		return nil
	case u.Scheme == "file" && s.d.AllowLocalRemotes:
		return nil
	}
	return invalid("invalid_repo_url", "仓库地址只收 https")
}

func checkRef(field, v string) error {
	if v == "" {
		return nil
	}
	if !gitRef.MatchString(v) || strings.HasPrefix(v, "-") || strings.Contains(v, "..") {
		return invalid("invalid_"+field, field+" 不合法")
	}
	return nil
}

func (s *Server) CreateWorkspace(ctx context.Context, req *connect.Request[workspacev1.CreateWorkspaceRequest]) (*connect.Response[workspacev1.CreateWorkspaceResponse], error) {
	m := req.Msg
	if err := s.checkRepoURL(m.GetRepoUrl()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	if err := checkRef("ref", m.GetRef()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.CreateWorkspaceResponse{}
	args := map[string]any{"repo_url": m.GetRepoUrl(), "ref": m.GetRef()}
	if err := s.operate(ctx, req.Header(), "workspace_create", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_CREATE, args, m.GetCredential(), out); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) GetDiff(ctx context.Context, req *connect.Request[workspacev1.GetDiffRequest]) (*connect.Response[workspacev1.GetDiffResponse], error) {
	m := req.Msg
	if err := checkRef("base", m.GetBase()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.GetDiffResponse{}
	if err := s.operate(ctx, req.Header(), "workspace_diff", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_DIFF, map[string]any{"base": m.GetBase()}, nil, out); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) CommitWorkspace(ctx context.Context, req *connect.Request[workspacev1.CommitWorkspaceRequest]) (*connect.Response[workspacev1.CommitWorkspaceResponse], error) {
	m := req.Msg
	switch {
	case strings.TrimSpace(m.GetMessage()) == "" || len(m.GetMessage()) > maxMessage || !utf8.ValidString(m.GetMessage()):
		return nil, taskapi.ConnectError(invalid("invalid_message", "提交说明不能为空，最多一万个字节"))
	case strings.ContainsAny(m.GetAuthorName()+m.GetAuthorEmail(), "<>\n\x00"):
		return nil, taskapi.ConnectError(invalid("invalid_author", "作者的名字或邮箱不合法"))
	}
	out := &workspacev1.CommitWorkspaceResponse{}
	args := map[string]any{"message": m.GetMessage(), "author_name": m.GetAuthorName(), "author_email": m.GetAuthorEmail()}
	if err := s.operate(ctx, req.Header(), "workspace_commit", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_COMMIT, args, nil, out); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) PushWorkspace(ctx context.Context, req *connect.Request[workspacev1.PushWorkspaceRequest]) (*connect.Response[workspacev1.PushWorkspaceResponse], error) {
	m := req.Msg
	if m.GetBranch() == "" {
		return nil, taskapi.ConnectError(invalid("invalid_branch", "要给远端的分支名"))
	}
	if err := checkRef("branch", m.GetBranch()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.PushWorkspaceResponse{}
	if err := s.operate(ctx, req.Header(), "workspace_push", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_PUSH, map[string]any{"branch": m.GetBranch()}, m.GetCredential(), out); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) ArchiveWorkspace(ctx context.Context, req *connect.Request[workspacev1.ArchiveWorkspaceRequest]) (*connect.Response[workspacev1.ArchiveWorkspaceResponse], error) {
	m := req.Msg
	out := &workspacev1.ArchiveWorkspaceResponse{}
	if err := s.operate(ctx, req.Header(), "workspace_archive", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_ARCHIVE, nil, nil, out); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) DeleteWorkspace(ctx context.Context, req *connect.Request[workspacev1.DeleteWorkspaceRequest]) (*connect.Response[workspacev1.DeleteWorkspaceResponse], error) {
	m := req.Msg
	out := &workspacev1.DeleteWorkspaceResponse{}
	if err := s.operate(ctx, req.Header(), "workspace_delete", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_DELETE, nil, nil, out); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	return connect.NewResponse(out), nil
}

// ListWorkspaces 直接读业务库的 workspaces（租户条件由存储负责）；有就绪环境的是 active，只有存档的是 archived。
func (s *Server) ListWorkspaces(ctx context.Context, req *connect.Request[workspacev1.ListWorkspacesRequest]) (*connect.Response[workspacev1.ListWorkspacesResponse], error) {
	_, tenant, err := s.caller(ctx, req.Header(), "workspace_list", req.Msg.GetTenantId(), "")
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	db, err := s.d.Store.Bind(tenant)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	var rows []relational.Row
	live := map[string]bool{}
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var err error
		rows, err = tx.Query(ctx, `SELECT workspace_id, repo_url, head, snapshot_id, size_bytes, files, created_at, updated_at
			FROM workspaces WHERE state <> 'deleted' ORDER BY workspace_id`)
		if err != nil {
			return err
		}
		envs, err := tx.Query(ctx, `SELECT DISTINCT workspace_id FROM sandbox_envs WHERE state = 'ready' AND workspace_id <> ''`)
		if err != nil && !sandboxgrants.MissingTable(err) {
			return err
		}
		for _, r := range envs {
			if w, ok := r["workspace_id"].(string); ok {
				live[w] = true
			}
		}
		return nil
	})
	if err != nil && sandboxgrants.MissingTable(err) {
		rows, err = nil, nil // Worker 还没迁移：还没有工作区
	}
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.ListWorkspacesResponse{}
	for _, r := range rows {
		out.Workspaces = append(out.Workspaces, decode(r, live))
	}
	return connect.NewResponse(out), nil
}

func decode(r relational.Row, live map[string]bool) *workspacev1.Workspace {
	str := func(k string) string { s, _ := r[k].(string); return s }
	num := func(k string) int64 {
		switch v := r[k].(type) {
		case int64:
			return v
		case float64:
			return int64(v)
		}
		return 0
	}
	ts := func(k string) *timestamppb.Timestamp {
		t, err := time.Parse(time.RFC3339Nano, str(k))
		if err != nil {
			return nil
		}
		return timestamppb.New(t)
	}
	w := &workspacev1.Workspace{
		WorkspaceId: str("workspace_id"), State: workspacev1.WorkspaceState_WORKSPACE_STATE_ACTIVE, RepoUrl: str("repo_url"), Head: str("head"),
		SnapshotId: str("snapshot_id"), SizeBytes: num("size_bytes"), Files: int32(num("files")), CreatedAt: ts("created_at"), UpdatedAt: ts("updated_at"),
	}
	if !live[w.WorkspaceId] && w.SnapshotId != "" {
		w.State = workspacev1.WorkspaceState_WORKSPACE_STATE_ARCHIVED
	}
	return w
}
