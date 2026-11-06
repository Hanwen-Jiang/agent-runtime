// Package workspaceapi 是 WorkspaceService 的 connect-go 实现（设计书改动稿 W2、新增 4.10.1；docs/plan/workspace-layer.md 第 5 节）：
// 与 Task API 同一个端口、同样认证。
//
// 租户由认证身份确定；请求里的 tenant_id 只用来校验一致（ingress.TenantContextFor）。别的租户的工作区一律 NotFound
// （Worker 按租户读业务库，查不到）。每个操作起一个 Python 的 WorkspaceOperationWorkflow（workspace 队列，ID wsop-<operation_id>）
// 并等它的结果；操作在承载工作区的沙箱里执行。只有 ListWorkspaces 直接读业务库（表由 Worker 迁移，还没有时为空）。
//
// 凭据（GitCredential）不进事件历史与日志：用 SecretStore 平台作用域的 workspace/credential-key 封成 wsc1（libs/go-core/sealing，
// 关联数据是租户与 operation_id），Worker 解开后只交给这一次的 git 命令。推送是 write_irreversible：这次调用本身是调用者的批准，
// 发起的用户作为批准人写进 gate 记录（Worker 一侧）。
package workspaceapi

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base32"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
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

	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/sealing"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1/workspacev1connect"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/workspaceready"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/sandboxgrants"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
)

// WorkflowName 是 Worker 一侧的工作流名，等于 names.proto 里 WORKFLOW_TYPE_WORKSPACE_OPERATION 的 wire_name（names_test.go 比对）。
const WorkflowName = "WorkspaceOperationWorkflow"

// TaskQueue 是操作工作流的任务队列（WS-READ-02）：单独一个 Worker 池，查看文件不和 Task 的 activity 抢 Worker。
// 等于 names.proto 里 TASK_QUEUE_WORKSPACE 的 wire_name。改之前起在 task 队列的操作照旧在那里跑完。
const TaskQueue = "workspace"

// WorkflowID 是一次操作的工作流 ID。
func WorkflowID(operationID string) string { return "wsop-" + operationID }

var (
	slug       = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
	gitRef     = regexp.MustCompile(`^[A-Za-z0-9._/@^~+-]{1,200}$`)
	opIDEncode = base32.NewEncoding("0123456789abcdefghjkmnpqrstvwxyz").WithPadding(base32.NoPadding)
)

const (
	maxURL          = 2048
	maxMessage      = 10_000
	maxInitialFiles = 200
	maxInitialBytes = 1 << 20
	maxLog          = 200
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
	// Cache 缓存只读操作的结果（WS-READ-03）：列目录、读文件、提交记录、diff 按（租户、工作区、generation、操作与参数）
	// 缓存；generation 由 Worker 在每次改动工作区后加一（业务库 workspaces.generation）。为 nil 时不缓存。
	Cache cache.Provider
	// ReadCacheTTL 是缓存的兜底有效期（工具之外改了工作区、generation 没变时最多旧这么久）；默认 5 分钟。
	ReadCacheTTL time.Duration
	// Sandbox 给了时列目录、读文件、提交记录、diff 直接调 Sandbox Service 的只读方法（WS-READ-01，SBX-01），不起 Worker 的
	// 工作流；task-service 是只读的调用方。为 nil 时照旧起工作流。
	Sandbox SandboxReader
}

// SandboxReader 是 Sandbox Service 的只读方法（sandboxv1connect.SandboxServiceClient 满足它）。
type SandboxReader interface {
	ListFiles(context.Context, *connect.Request[sandboxv1.ListFilesRequest]) (*connect.Response[sandboxv1.ListFilesResponse], error)
	ReadFile(context.Context, *connect.Request[sandboxv1.ReadFileRequest]) (*connect.Response[sandboxv1.ReadFileResponse], error)
	GetDiff(context.Context, *connect.Request[sandboxv1.GetDiffRequest]) (*connect.Response[sandboxv1.GetDiffResponse], error)
	GetLog(context.Context, *connect.Request[sandboxv1.GetLogRequest]) (*connect.Response[sandboxv1.GetLogResponse], error)
}

// maxCachedResult 是缓存的单个结果的上限（字节，protojson）；更大的不缓存。
const maxCachedResult = 256 << 10

// Server 实现 workspacev1connect.WorkspaceServiceHandler。
type Server struct{ d Deps }

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
	if d.ReadCacheTTL <= 0 {
		d.ReadCacheTTL = 5 * time.Minute
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
	return s.run(ctx, id, tenant, workspaceID, op, args, credential, out)
}

// operateRead 同 operate，只用于只读操作：工作区在业务库里有行、配了 Cache 时先查缓存（WS-READ-03）。
func (s *Server) operateRead(
	ctx context.Context, header http.Header, route, requestTenant, workspaceID string, op workflowv1.WorkspaceOperation,
	args map[string]any, out proto.Message,
) error {
	if workspaceID == "" {
		return invalid("invalid_workspace", "要给 workspace_id")
	}
	id, tenant, err := s.caller(ctx, header, route, requestTenant, workspaceID)
	if err != nil {
		return err
	}
	c, key := s.readCache(ctx, tenant, workspaceID, op, args)
	if c != nil {
		if data, ok, err := c.Get(ctx, key); err == nil && ok && protojson.Unmarshal(data, out) == nil {
			return nil
		}
	}
	if s.d.Sandbox != nil && args["repo"] == nil && !s.multiLayout(ctx, tenant, workspaceID) {
		if err := s.readFromSandbox(ctx, tenant, workspaceID, op, args, out); err != nil {
			return err
		}
	} else if err := s.run(ctx, id, tenant, workspaceID, op, args, nil, out); err != nil {
		return err
	}
	if c != nil {
		if data, err := protojson.Marshal(out); err == nil && len(data) <= maxCachedResult {
			_ = c.Set(ctx, key, data, s.d.ReadCacheTTL)
		}
	}
	return nil
}

// readFromSandbox 调 Sandbox Service 的只读方法（WS-READ-01）：租户是服务端按认证身份定的，服务的结果原样写进 out；
// 服务的错误按统一错误模型解出来（ar-error-bin）。
func (s *Server) readFromSandbox(ctx context.Context, tenant *commonv1.TenantContext, workspaceID string, op workflowv1.WorkspaceOperation, args map[string]any, out proto.Message) error {
	str := func(k string) string { v, _ := args[k].(string); return v }
	var got proto.Message
	var err error
	switch op {
	case workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LIST_FILES:
		var r *connect.Response[sandboxv1.ListFilesResponse]
		if r, err = s.d.Sandbox.ListFiles(ctx, connect.NewRequest(&sandboxv1.ListFilesRequest{Tenant: tenant, WorkspaceId: workspaceID, Path: str("path")})); err == nil {
			got = r.Msg.GetResponse()
		}
	case workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_READ_FILE:
		var r *connect.Response[sandboxv1.ReadFileResponse]
		if r, err = s.d.Sandbox.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tenant, WorkspaceId: workspaceID, Path: str("path")})); err == nil {
			got = r.Msg.GetResponse()
		}
	case workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_DIFF:
		renames, _ := args["find_renames"].(bool)
		var r *connect.Response[sandboxv1.GetDiffResponse]
		if r, err = s.d.Sandbox.GetDiff(ctx, connect.NewRequest(&sandboxv1.GetDiffRequest{Tenant: tenant, WorkspaceId: workspaceID, Base: str("base"), FindRenames: renames})); err == nil {
			got = r.Msg.GetResponse()
		}
	case workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LOG:
		limit, _ := args["limit"].(float64)
		var r *connect.Response[sandboxv1.GetLogResponse]
		if r, err = s.d.Sandbox.GetLog(ctx, connect.NewRequest(&sandboxv1.GetLogRequest{Tenant: tenant, WorkspaceId: workspaceID, Limit: int32(limit), Base: str("base")})); err == nil {
			got = r.Msg.GetResponse()
		}
	default:
		return invalid("invalid_operation", "这个操作不是只读的")
	}
	if err != nil {
		if e := connecterr.Decode(err); e != nil {
			return e
		}
		return providererr.Wrap(providererr.Transient, "sandbox_service_unavailable", "Sandbox Service 暂时不可用，稍后再试", err)
	}
	if got == nil {
		return nil
	}
	proto.Merge(out, got)
	return nil
}

// readCache 返回租户的 Cache 与这次读的键；不缓存时（没配 Cache、业务库里没有这个工作区、查不了版本号）返回 nil。
// 键里有工作区、版本号、操作与参数的哈希；租户由 Cache 的绑定隔开。
func (s *Server) readCache(ctx context.Context, tenant *commonv1.TenantContext, workspaceID string, op workflowv1.WorkspaceOperation, args map[string]any) (cache.Cache, string) {
	if s.d.Cache == nil {
		return nil, ""
	}
	db, err := s.d.Store.Bind(tenant)
	if err != nil {
		return nil, ""
	}
	var generation string
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, `SELECT generation FROM workspaces WHERE workspace_id = $1 AND state <> 'deleted'`, workspaceID)
		if err == nil && len(rows) == 1 {
			generation = fmt.Sprint(rows[0]["generation"])
		}
		return err
	})
	if err != nil || generation == "" {
		return nil, ""
	}
	c, err := s.d.Cache.Bind(tenant)
	if err != nil {
		return nil, ""
	}
	raw, _ := json.Marshal(args)
	sum := sha256.Sum256(raw)
	return c, "wsread:" + workspaceID + ":" + generation + ":" + op.String() + ":" + hex.EncodeToString(sum[:16])
}

// run 起一次工作区操作的工作流并等结果，结果按 protojson 写进 out。
func (s *Server) run(
	ctx context.Context, id ingress.Identity, tenant *commonv1.TenantContext, workspaceID string, op workflowv1.WorkspaceOperation,
	args map[string]any, credential *workspacev1.GitCredential, out proto.Message,
) error {
	var err error
	operationID := s.d.NewOperationID()
	async, _ := ctx.Value(asyncKey{}).(*asyncStart)
	if async != nil {
		operationID = "wso-" + hashOperation(tenant.GetTenantId(), id.UserID, async.requestID)
	}
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
	if async != nil {
		if in.Args == nil {
			in.Args = &structpb.Struct{Fields: map[string]*structpb.Value{}}
		}
		in.Args.Fields["__async_result"] = structpb.NewBoolValue(true)
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
	execution := s.d.Timeout + 30*time.Minute
	if async != nil {
		return s.startAsync(ctx, id, tenant, engine, in, execution, out, async)
	}
	// WS-CREATE-RACE：建工作区期间不收在它上面的 Task（taskapi 的 submit 查这个标记）。
	creating, err := s.markCreating(ctx, tenant, op, workspaceID, operationID, execution)
	if err != nil {
		return err
	}
	if _, err := engine.StartWorkflow(ctx, WorkflowName, durable.StartOptions{
		WorkflowID: WorkflowID(operationID), TaskQueue: TaskQueue, Input: in, ExecutionTimeout: execution,
	}); err != nil {
		if creating != nil {
			_ = workspaceready.Clear(context.WithoutCancel(ctx), creating, workspaceID, operationID)
		}
		return err
	}
	if creating != nil {
		// 等工作流真正结束再清：客户端断开（例如经 Cloudflare 的 100 秒）只结束这次请求，工作流照样在跑。
		go func(bg context.Context) {
			_, _ = engine.Result(bg, WorkflowID(operationID), &workflowv1.WorkspaceOperationResult{}, execution)
			_ = workspaceready.Clear(bg, creating, workspaceID, operationID)
		}(context.WithoutCancel(ctx))
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

// markCreating 只给建工作区记“在建”的标记（WS-CREATE-RACE）；没配 Cache 时不记，返回 nil。
func (s *Server) markCreating(ctx context.Context, tenant *commonv1.TenantContext, op workflowv1.WorkspaceOperation, workspaceID, operationID string, ttl time.Duration) (cache.Cache, error) {
	if op != workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_CREATE || s.d.Cache == nil {
		return nil, nil
	}
	c, err := s.d.Cache.Bind(tenant)
	if err != nil {
		return nil, err
	}
	if err := workspaceready.Mark(ctx, c, workspaceID, operationID, ttl); err != nil {
		return nil, err
	}
	return c, nil
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

// checkPath 是路径的形式检查（相对、/ 分隔、没有 . 与 .. 段、不碰 .git）；Worker 那边再按同样的规则核一遍。
func checkPath(p string, allowRoot bool) error {
	if p == "" || p == "." {
		if allowRoot {
			return nil
		}
		return invalid("invalid_path", "要给工作区里的一个文件")
	}
	if strings.ContainsAny(p, "\\\x00") || strings.HasPrefix(p, "/") || (len(p) > 1 && p[1] == ':') {
		return invalid("invalid_path", "路径要相对工作区根，用 / 分隔")
	}
	for _, part := range strings.Split(p, "/") {
		switch part {
		case "", ".", "..":
			return invalid("invalid_path", "路径不得含空段、. 或 ..")
		case ".git":
			return invalid("invalid_path", "不能直接访问 .git")
		}
	}
	return nil
}

func checkAuthor(name, email string) error {
	if strings.ContainsAny(name+email, "<>\n\x00") || len(name) > 200 || len(email) > 200 {
		return invalid("invalid_author", "作者的名字或邮箱不合法")
	}
	return nil
}

func (s *Server) CreateWorkspace(ctx context.Context, req *connect.Request[workspacev1.CreateWorkspaceRequest]) (*connect.Response[workspacev1.CreateWorkspaceResponse], error) {
	m := req.Msg
	if m.GetLayout() == workspacev1.WorkspaceLayout_WORKSPACE_LAYOUT_MULTI && (m.GetRepoUrl() != "" || m.GetRef() != "" || len(m.GetFiles()) != 0) {
		return nil, taskapi.ConnectError(invalid("conflicting_sources", "multi 工作区通过 AttachRepo 加入仓库"))
	}
	if m.GetLayout() < 0 || m.GetLayout() > workspacev1.WorkspaceLayout_WORKSPACE_LAYOUT_MULTI {
		return nil, taskapi.ConnectError(invalid("invalid_layout", "工作区布局不合法"))
	}
	if err := s.checkRepoURL(m.GetRepoUrl()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	if err := checkRef("ref", m.GetRef()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	// files 可以与 repo_url 同给（克隆之后写入，另做一次提交；TMP 计划 eval-observability-and-runtime-fixes 11.10）。
	switch {
	case m.GetFromWorkspaceId() != "" && (m.GetRepoUrl() != "" || len(m.GetFiles()) > 0):
		return nil, taskapi.ConnectError(invalid("conflicting_sources", "from_workspace_id 不能与 repo_url、files 同给"))
	case m.GetFromWorkspaceId() != "" && (!slug.MatchString(m.GetFromWorkspaceId()) || m.GetFromWorkspaceId() == m.GetWorkspaceId()):
		return nil, taskapi.ConnectError(invalid("invalid_source", "from_workspace_id 要是同租户里另一个工作区"))
	case len(m.GetFiles()) > maxInitialFiles:
		return nil, taskapi.ConnectError(invalid("invalid_files", "files 最多 200 个"))
	}
	if err := checkAuthor(m.GetAuthorName(), m.GetAuthorEmail()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	files := make([]any, 0, len(m.GetFiles()))
	total := 0
	for _, f := range m.GetFiles() {
		if err := checkPath(f.GetPath(), false); err != nil {
			return nil, taskapi.ConnectError(err)
		}
		total += len(f.GetContent())
		files = append(files, map[string]any{"path": f.GetPath(), "content": f.GetContent()})
	}
	if total > maxInitialBytes {
		return nil, taskapi.ConnectError(invalid("invalid_files", "files 合计最多 1 MiB"))
	}
	out := &workspacev1.CreateWorkspaceResponse{}
	args := map[string]any{
		"repo_url": m.GetRepoUrl(), "ref": m.GetRef(), "from_workspace_id": m.GetFromWorkspaceId(), "files": files,
		"author_name": m.GetAuthorName(), "author_email": m.GetAuthorEmail(),
	}
	if m.GetLayout() == workspacev1.WorkspaceLayout_WORKSPACE_LAYOUT_MULTI {
		args["layout"] = "multi"
	}
	if env := m.GetEnvironment(); env != nil {
		// ENV-02：选用的环境配置随建工作区交给 Worker（只有密钥的名字），见 environment.go。
		if err := checkEnvironment(env); err != nil {
			return nil, taskapi.ConnectError(err)
		}
		doc, err := environmentArgs(env)
		if err != nil {
			return nil, taskapi.ConnectError(invalid("invalid_environment", "环境配置的格式不对"))
		}
		args["environment"] = doc
	}
	if err := s.operate(ctx, req.Header(), "workspace_create", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_CREATE, args, m.GetCredential(), out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) GetDiff(ctx context.Context, req *connect.Request[workspacev1.GetDiffRequest]) (*connect.Response[workspacev1.GetDiffResponse], error) {
	m := req.Msg
	if err := checkRef("base", m.GetBase()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.GetDiffResponse{}
	args := map[string]any{"base": m.GetBase(), "find_renames": m.GetFindRenames()}
	if m.GetRepo() != "" {
		args["repo"] = m.GetRepo()
	}
	if err := s.operateRead(ctx, req.Header(), "workspace_diff", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_DIFF, args, out); err != nil {
		return nil, workspaceError(err)
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
	if m.GetRepo() != "" {
		args["repo"] = m.GetRepo()
	}
	if err := s.operate(ctx, req.Header(), "workspace_commit", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_COMMIT, args, nil, out); err != nil {
		return nil, workspaceError(err)
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
	if err := s.operate(ctx, req.Header(), "workspace_push", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_PUSH, map[string]any{"branch": m.GetBranch(), "repo": m.GetRepo()}, m.GetCredential(), out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) ArchiveWorkspace(ctx context.Context, req *connect.Request[workspacev1.ArchiveWorkspaceRequest]) (*connect.Response[workspacev1.ArchiveWorkspaceResponse], error) {
	m := req.Msg
	out := &workspacev1.ArchiveWorkspaceResponse{}
	if err := s.operate(ctx, req.Header(), "workspace_archive", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_ARCHIVE, nil, nil, out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) DeleteWorkspace(ctx context.Context, req *connect.Request[workspacev1.DeleteWorkspaceRequest]) (*connect.Response[workspacev1.DeleteWorkspaceResponse], error) {
	m := req.Msg
	out := &workspacev1.DeleteWorkspaceResponse{}
	if err := s.operate(ctx, req.Header(), "workspace_delete", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_DELETE, nil, nil, out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) ListFiles(ctx context.Context, req *connect.Request[workspacev1.ListFilesRequest]) (*connect.Response[workspacev1.ListFilesResponse], error) {
	m := req.Msg
	if err := checkPath(m.GetPath(), true); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.ListFilesResponse{}
	if err := s.operateRead(ctx, req.Header(), "workspace_read", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LIST_FILES, map[string]any{"path": m.GetPath()}, out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) ReadFile(ctx context.Context, req *connect.Request[workspacev1.ReadFileRequest]) (*connect.Response[workspacev1.ReadFileResponse], error) {
	m := req.Msg
	if err := checkPath(m.GetPath(), false); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.ReadFileResponse{}
	if err := s.operateRead(ctx, req.Header(), "workspace_read", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_READ_FILE, map[string]any{"path": m.GetPath()}, out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) GetLog(ctx context.Context, req *connect.Request[workspacev1.GetLogRequest]) (*connect.Response[workspacev1.GetLogResponse], error) {
	m := req.Msg
	if m.GetLimit() < 0 || m.GetLimit() > maxLog {
		return nil, taskapi.ConnectError(invalid("invalid_limit", "limit 最多 200"))
	}
	if err := checkRef("base", m.GetBase()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.GetLogResponse{}
	args := map[string]any{"limit": float64(m.GetLimit()), "base": m.GetBase()}
	if m.GetRepo() != "" {
		args["repo"] = m.GetRepo()
	}
	if err := s.operateRead(ctx, req.Header(), "workspace_read", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_LOG, args, out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) DiscardChanges(ctx context.Context, req *connect.Request[workspacev1.DiscardChangesRequest]) (*connect.Response[workspacev1.DiscardChangesResponse], error) {
	m := req.Msg
	out := &workspacev1.DiscardChangesResponse{}
	if err := s.operate(ctx, req.Header(), "workspace_discard", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_DISCARD, map[string]any{"repo": m.GetRepo()}, nil, out); err != nil {
		return nil, workspaceError(err)
	}
	return connect.NewResponse(out), nil
}

func (s *Server) SyncWorkspace(ctx context.Context, req *connect.Request[workspacev1.SyncWorkspaceRequest]) (*connect.Response[workspacev1.SyncWorkspaceResponse], error) {
	m := req.Msg
	if m.GetBranch() == "" {
		return nil, taskapi.ConnectError(invalid("invalid_branch", "要给远端的分支名"))
	}
	if err := checkRef("branch", m.GetBranch()); err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.SyncWorkspaceResponse{}
	if err := s.operate(ctx, req.Header(), "workspace_sync", m.GetTenantId(), m.GetWorkspaceId(), workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_SYNC, map[string]any{"branch": m.GetBranch(), "repo": m.GetRepo()}, m.GetCredential(), out); err != nil {
		return nil, workspaceError(err)
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
	read := func(columns string) error {
		return db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
			var err error
			rows, err = tx.Query(ctx, `SELECT workspace_id, repo_url, head, snapshot_id, size_bytes, files, created_at, updated_at`+columns+`
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
	}
	err = read(", environment_id, layout")
	if missingColumn(err) {
		err = read(", environment_id")
	}
	if missingColumn(err) {
		// Worker 还没迁移 207（ENV-02）：没有 environment_id 列，另开一个事务按旧的列读（Postgres 上出错的事务不能再用）。
		err = read("")
	}
	if err != nil && sandboxgrants.MissingTable(err) {
		rows, err = nil, nil // Worker 还没迁移：还没有工作区
	}
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.ListWorkspacesResponse{}
	for _, r := range rows {
		w := decode(r, live)
		if w.Layout == workspacev1.WorkspaceLayout_WORKSPACE_LAYOUT_MULTI {
			if err := db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
				repos, e := tx.Query(ctx, `SELECT name, url, ref, path, access, is_primary, head, branch, attached_at FROM workspace_repos WHERE workspace_id = $1 ORDER BY path`, w.WorkspaceId)
				if e != nil {
					return e
				}
				for _, repo := range repos {
					w.Repos = append(w.Repos, decodeRepo(repo))
				}
				return nil
			}); err != nil {
				return nil, taskapi.ConnectError(err)
			}
		}
		out.Workspaces = append(out.Workspaces, w)
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
		EnvironmentId: str("environment_id"), Layout: workspacev1.WorkspaceLayout_WORKSPACE_LAYOUT_ROOT,
	}
	if str("layout") == "multi" {
		w.Layout = workspacev1.WorkspaceLayout_WORKSPACE_LAYOUT_MULTI
	}
	if !live[w.WorkspaceId] && w.SnapshotId != "" {
		w.State = workspacev1.WorkspaceState_WORKSPACE_STATE_ARCHIVED
	}
	return w
}
