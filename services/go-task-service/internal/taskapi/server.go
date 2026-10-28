// Package taskapi 是 Task API 的 connect-go 实现（设计书 3.1.1、4.15）：提交、取消、信号、查询。
// 同一端口既是 gRPC，也接受 HTTP + JSON。
//
// 租户由 Ingress 根据认证身份确定；请求里的 tenant_id 只用来校验一致（ingress.TenantContextFor）。
// 所有 Provider 都按这个租户绑定，访问别的租户的 Session 或 Task 一律返回 NotFound。
// 取消与人工回复（包括 write_irreversible 的审批）只接受 Task 的提交者，或身份带 ScopeApprove 的用户
// （R-05 评审 D9）；同租户的其他人 PermissionDenied。查询对同租户开放。
package taskapi

import (
	"context"
	"errors"
	"math"
	"net/http"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode"

	"connectrpc.com/connect"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1/taskapiv1connect"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/events"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/ids"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

// 限流的维度（ingress.Ingress.Admit 的 route）。
const (
	routeSubmit = "submit_task"
	routeCancel = "cancel_task"
	routeSignal = "signal_task"
	routeQuery  = "query_task"
)

const (
	maxClientRequestID = 128
	maxMessageBytes    = 256 << 10
)

// ScopeApprove 让持有者可以审批、回复与取消同租户里别人提交的 Task。
const ScopeApprove = "task.approve"

var objectURI = regexp.MustCompile(`^obj://([a-z0-9][a-z0-9_-]{0,62})/\S+$`)

// PinResolver 给出新 Task 固定的版本（设计书 3.1.2“固定”）。Registry 实现之前用 FilePinResolver。
type PinResolver interface {
	Resolve(ctx context.Context, tenant *commonv1.TenantContext) (*workflowv1.PinnedVersions, error)
}

// FilePinResolver 每次都重新读取一个 PinnedVersions 的 protojson 文件（开发实现）。文件不存在时版本为空。
type FilePinResolver struct{ Path string }

func (r FilePinResolver) Resolve(_ context.Context, _ *commonv1.TenantContext) (*workflowv1.PinnedVersions, error) {
	pins := &workflowv1.PinnedVersions{}
	if r.Path == "" {
		return pins, nil
	}
	data, err := os.ReadFile(r.Path)
	if errors.Is(err, os.ErrNotExist) {
		return pins, nil
	}
	if err != nil {
		return nil, providererr.Wrap(providererr.Transient, "pins_unavailable", "读不到固定版本文件", err)
	}
	if err := protojson.Unmarshal(data, pins); err != nil {
		return nil, providererr.Wrap(providererr.Invalid, "pins_malformed", "固定版本文件格式不对", err)
	}
	return pins, nil
}

// Defaults 是租户配额与策略接入之前的默认值。
type Defaults struct {
	Budget        *workflowv1.Budget
	EffectCeiling commonv1.Effect
}

// Deps 是 Server 的依赖。
type Deps struct {
	Ingress ingress.Ingress
	Durable durable.Provider
	Store   relational.Provider
	Pins    PinResolver
	Clock   clock.Clock
	IDs     ids.Generator
	// PublicBaseURL 用来拼 SSE 订阅地址，例如 https://api.example.com。
	PublicBaseURL string
	Defaults      Defaults
	// Policies 按租户给出预算与 effect 上限；为空时所有租户都用 Defaults。
	Policies PolicyResolver
	// Network 给出租户策略的 sandbox.network（提交时的 network_request 按它核对，D-16）；为空时用 Policies（它实现了
	// NetworkPolicySource 的话），否则谁都不能申请联网。
	Network NetworkPolicySource
}

// Server 实现 taskapiv1connect.TaskServiceHandler。
type Server struct {
	d     Deps
	tasks *taskstore.Store
}

func (s *Server) policies() PolicyResolver {
	if s.d.Policies != nil {
		return s.d.Policies
	}
	return StaticPolicy{Defaults: s.d.Defaults}
}

func (s *Server) networkPolicy(ctx context.Context, tenant *commonv1.TenantContext) (NetworkPolicy, error) {
	if s.d.Network != nil {
		return s.d.Network.NetworkPolicy(ctx, tenant)
	}
	if src, ok := s.policies().(NetworkPolicySource); ok {
		return src.NetworkPolicy(ctx, tenant)
	}
	return NetworkPolicy{}, nil
}

var _ taskapiv1connect.TaskServiceHandler = (*Server)(nil)

// New 校验依赖并构造 Server。
func New(d Deps) (*Server, error) {
	switch {
	case d.Ingress == nil, d.Durable == nil, d.Store == nil, d.Pins == nil, d.IDs == nil:
		return nil, errors.New("taskapi: Ingress、Durable、Store、Pins、IDs 都不能为空")
	case d.PublicBaseURL == "":
		return nil, errors.New("taskapi: 缺少 PublicBaseURL")
	}
	if d.Clock == nil {
		d.Clock = clock.System{}
	}
	return &Server{d: d, tasks: taskstore.New(d.Store)}, nil
}

// Mount 把 Task API 挂到 mux 上。
func (s *Server) Mount(mux *http.ServeMux, opts ...connect.HandlerOption) {
	path, handler := taskapiv1connect.NewTaskServiceHandler(s, opts...)
	mux.Handle(path, handler)
}

func invalid(msg string) error { return providererr.New(providererr.Invalid, "invalid_argument", msg) }

func missing(msg string) error { return providererr.New(providererr.NotFound, "not_found", msg) }

// authenticate 认证并按路由限流，返回认证身份。
func (s *Server) authenticate(ctx context.Context, header http.Header, route string) (ingress.Identity, error) {
	id, err := s.d.Ingress.Authenticate(ctx, ingress.Credentials{BearerToken: events.BearerToken(header.Get("Authorization"))})
	if err != nil {
		return ingress.Identity{}, err
	}
	if err := s.d.Ingress.Admit(ctx, id, route); err != nil {
		return ingress.Identity{}, err
	}
	return id, nil
}

func printable(s string) bool {
	for _, r := range s {
		if unicode.IsControl(r) {
			return false
		}
	}
	return true
}

// lookup 认证、校验 task_id，并在该租户下找到 Task。
func (s *Server) lookup(ctx context.Context, header http.Header, route, requestTenant, taskID string) (ingress.Identity, *commonv1.TenantContext, taskstore.Task, error) {
	id, err := s.authenticate(ctx, header, route)
	if err != nil {
		return id, nil, taskstore.Task{}, err
	}
	if !ids.ValidTaskID(taskID) {
		return id, nil, taskstore.Task{}, invalid("task_id 格式不对")
	}
	tenant, err := ingress.TenantContextFor(id, requestTenant, "", "", nil)
	if err != nil {
		return id, nil, taskstore.Task{}, err
	}
	row, ok, err := s.tasks.LookupTask(ctx, tenant, taskID)
	if err != nil {
		return id, nil, taskstore.Task{}, err
	}
	if !ok {
		return id, nil, taskstore.Task{}, missing("Task 不存在")
	}
	tenant.SessionId = row.SessionID
	return id, tenant, row, nil
}

// mayControl：取消与人工回复只接受提交者或带 ScopeApprove 的用户。没记下提交者的旧 Task 只接受后者。
func mayControl(id ingress.Identity, row taskstore.Task) error {
	if (row.SubmittedBy != "" && row.SubmittedBy == id.UserID) || slices.Contains(id.Scopes, ScopeApprove) {
		return nil
	}
	return providererr.New(providererr.Denied, "not_task_owner", "只有提交者或有审批权限的用户能取消这个 Task 或回复它的问题")
}

func (s *Server) SubmitTask(ctx context.Context, req *connect.Request[taskapiv1.SubmitTaskRequest]) (*connect.Response[taskapiv1.SubmitTaskResponse], error) {
	resp, err := s.submit(ctx, req.Header(), req.Msg)
	if err != nil {
		return nil, ConnectError(err)
	}
	return connect.NewResponse(resp), nil
}

func (s *Server) submit(ctx context.Context, header http.Header, req *taskapiv1.SubmitTaskRequest) (*taskapiv1.SubmitTaskResponse, error) {
	id, err := s.authenticate(ctx, header, routeSubmit)
	if err != nil {
		return nil, err
	}
	crid := req.GetClientRequestId()
	switch {
	case crid == "" || len(crid) > maxClientRequestID || !printable(crid):
		return nil, invalid("client_request_id 必须是 1 到 128 个可打印字符")
	case strings.TrimSpace(req.GetMessage()) == "":
		return nil, invalid("命令不能为空")
	case len(req.GetMessage()) > maxMessageBytes:
		return nil, invalid("命令过长")
	case req.GetSessionId() != "" && !ids.ValidSessionID(req.GetSessionId()):
		return nil, invalid("session_id 格式不对")
	}
	tenant, err := ingress.TenantContextFor(id, req.GetTenantId(), req.GetWorkspaceId(), "", nil)
	if err != nil {
		return nil, err
	}
	for _, uri := range req.GetAttachments() {
		m := objectURI.FindStringSubmatch(uri)
		if m == nil {
			return nil, invalid("附件必须是 obj:// 引用")
		}
		if m[1] != tenant.GetTenantId() {
			return nil, providererr.New(providererr.Denied, "foreign_object", "附件不属于当前租户")
		}
	}
	// 请求里的 effect 上限只能调低（docs/plan/cloud-deploy.md D-08）：在建 Task 之前核对，被拒的请求不留记录。
	if want := req.GetEffectCeiling(); want != commonv1.Effect_EFFECT_UNSPECIFIED {
		if _, known := commonv1.Effect_name[int32(want)]; !known {
			return nil, invalid("effect_ceiling 不是附录 A 的 effect")
		}
		policy, err := s.policies().Resolve(ctx, tenant)
		if err != nil {
			return nil, err
		}
		if want > policy.EffectCeiling {
			return nil, providererr.New(providererr.Denied, "effect_ceiling_exceeds_policy", "请求的 effect 上限高于租户策略允许的")
		}
	}
	// 联网申请（D-16）同样在建 Task 之前按租户策略核对；批准的申请在投递之前写成这个 Task 的联网授权。
	var network *checkedNetwork
	var networkPolicy NetworkPolicy
	if req.GetNetworkRequest() != nil {
		policy, err := s.policies().Resolve(ctx, tenant)
		if err != nil {
			return nil, err
		}
		ceiling := policy.EffectCeiling
		if want := req.GetEffectCeiling(); want != commonv1.Effect_EFFECT_UNSPECIFIED && want < ceiling {
			ceiling = want
		}
		if networkPolicy, err = s.networkPolicy(ctx, tenant); err != nil {
			return nil, err
		}
		checked, err := CheckNetworkRequest(networkPolicy, req.GetNetworkRequest(), ceiling, id.Scopes)
		if err != nil {
			return nil, err
		}
		network = &checked
	}

	now := s.d.Clock.Now()
	sessionID := req.GetSessionId()
	if sessionID != "" {
		ok, err := s.tasks.SessionExists(ctx, tenant, sessionID)
		if err != nil {
			return nil, err
		}
		if !ok {
			return nil, missing("Session 不存在")
		}
	} else {
		sessionID = s.d.IDs.SessionID()
	}
	row, existed, err := s.tasks.InsertTask(ctx, tenant, taskstore.Task{
		TaskID: s.d.IDs.TaskID(), SessionID: sessionID, SessionKey: req.GetSessionId(), ClientRequestID: crid, CreatedAt: now,
		SubmittedBy: id.UserID,
	})
	if err != nil {
		return nil, err
	}
	resp := &taskapiv1.SubmitTaskResponse{
		TaskId: row.TaskID, SessionId: row.SessionID, EventsUrl: events.URL(s.d.PublicBaseURL, row.TaskID), Deduplicated: existed,
	}
	if existed && row.Signaled {
		return resp, nil
	}
	// 第一次提交，或上一次的信号没有送达：（再）投递。Session 工作流按 client_request_id 去重。
	if req.GetSessionId() == "" {
		if err := s.tasks.CreateSession(ctx, tenant, row.SessionID, now); err != nil {
			return nil, err
		}
	}
	tenant.SessionId = row.SessionID
	pins, err := s.d.Pins.Resolve(ctx, tenant)
	if err != nil {
		return nil, err
	}
	policy, err := s.policies().Resolve(ctx, tenant)
	if err != nil {
		return nil, err
	}
	ceiling := policy.EffectCeiling
	if want := req.GetEffectCeiling(); want != commonv1.Effect_EFFECT_UNSPECIFIED && want <= ceiling {
		ceiling = want
	}
	task := &workflowv1.TaskWorkflowInput{
		Tenant:         tenant,
		TaskId:         row.TaskID,
		PinnedVersions: pins,
		Message:        req.GetMessage(),
		Inputs:         req.GetInputs(),
		Attachments:    req.GetAttachments(),
		SubmittedAt:    timestamppb.New(now),
		SessionAccess:  req.GetSessionAccess(),
		Priority:       req.GetPriority(),
		EffectCeiling:  ceiling,
		Budget:         policy.Budget,
	}
	if task.SessionAccess == commonv1.SessionAccess_SESSION_ACCESS_UNSPECIFIED {
		// 不知道会不会写 Session 状态时按会写处理，串行最安全。
		task.SessionAccess = commonv1.SessionAccess_SESSION_ACCESS_READ_WRITE
	}
	if task.Priority == commonv1.Priority_PRIORITY_UNSPECIFIED {
		task.Priority = commonv1.Priority_PRIORITY_INTERACTIVE
	}
	if network != nil {
		if err := writeSubmitGrant(ctx, s.d.Store, tenant, row.TaskID, *network, networkPolicy, id.UserID, now); err != nil {
			return nil, err
		}
	}
	engine, err := s.d.Durable.Bind(tenant)
	if err != nil {
		return nil, err
	}
	_, err = engine.SignalWithStart(ctx, session.WorkflowName, durable.StartOptions{
		WorkflowID: row.SessionID,
		TaskQueue:  session.SessionTaskQueue,
		Input:      &workflowv1.SessionWorkflowInput{Tenant: proto.Clone(tenant).(*commonv1.TenantContext)},
	}, session.SignalSubmitTask, &workflowv1.SubmitTaskSignal{ClientRequestId: crid, Task: task})
	if err != nil {
		if providererr.CategoryOf(err) == providererr.Conflict {
			// 同 ID 的工作流属于别的作用域：对调用方而言就是不存在。
			return nil, missing("Session 不存在")
		}
		return nil, err
	}
	if err := s.tasks.MarkSignaled(ctx, tenant, row.TaskID); err != nil {
		return nil, err
	}
	return resp, nil
}

func (s *Server) CancelTask(ctx context.Context, req *connect.Request[taskapiv1.CancelTaskRequest]) (*connect.Response[taskapiv1.CancelTaskResponse], error) {
	id, tenant, row, err := s.lookup(ctx, req.Header(), routeCancel, req.Msg.GetTenantId(), req.Msg.GetTaskId())
	if err != nil {
		return nil, ConnectError(err)
	}
	if err := mayControl(id, row); err != nil {
		return nil, ConnectError(err)
	}
	engine, err := s.d.Durable.Bind(tenant)
	if err != nil {
		return nil, ConnectError(err)
	}
	if !terminal(row.Status) {
		// 排队中的由 Session 工作流移出队列，运行中的由它取消子工作流（设计书 3.1.1 第 5 条）。
		if err := engine.Signal(ctx, row.SessionID, session.SignalCancelTask,
			&workflowv1.CancelTaskSignal{TaskId: row.TaskID, Reason: req.Msg.GetReason()}); err != nil {
			return nil, ConnectError(err)
		}
	}
	status, _, _ := s.status(ctx, engine, row)
	return connect.NewResponse(&taskapiv1.CancelTaskResponse{Status: status}), nil
}

func (s *Server) SignalTask(ctx context.Context, req *connect.Request[taskapiv1.SignalTaskRequest]) (*connect.Response[taskapiv1.SignalTaskResponse], error) {
	id, tenant, row, err := s.lookup(ctx, req.Header(), routeSignal, req.Msg.GetTenantId(), req.Msg.GetTaskId())
	if err != nil {
		return nil, ConnectError(err)
	}
	if err := mayControl(id, row); err != nil {
		return nil, ConnectError(err)
	}
	reply := req.Msg.GetHumanReply()
	if reply == nil {
		return nil, ConnectError(invalid("没有信号内容"))
	}
	if reply.GetQuestionId() == "" || reply.GetOption() == "" {
		return nil, ConnectError(invalid("人工回复必须带 question_id 与 option"))
	}
	if terminal(row.Status) {
		return nil, connect.NewError(connect.CodeFailedPrecondition, errors.New("Task 已经结束"))
	}
	engine, err := s.d.Durable.Bind(tenant)
	if err != nil {
		return nil, ConnectError(err)
	}
	err = engine.Signal(ctx, row.TaskID, session.SignalHumanReply, &workflowv1.HumanReplySignal{
		QuestionId:      reply.GetQuestionId(),
		Option:          reply.GetOption(),
		Guidance:        reply.GetGuidance(),
		RepliedByUserId: id.UserID,
		RepliedAt:       timestamppb.New(s.d.Clock.Now()),
	})
	if providererr.CategoryOf(err) == providererr.NotFound {
		return nil, connect.NewError(connect.CodeFailedPrecondition, errors.New("Task 还没开始运行"))
	}
	if err != nil {
		return nil, ConnectError(err)
	}
	return connect.NewResponse(&taskapiv1.SignalTaskResponse{}), nil
}

func (s *Server) QueryTask(ctx context.Context, req *connect.Request[taskapiv1.QueryTaskRequest]) (*connect.Response[taskapiv1.QueryTaskResponse], error) {
	_, tenant, row, err := s.lookup(ctx, req.Header(), routeQuery, req.Msg.GetTenantId(), req.Msg.GetTaskId())
	if err != nil {
		return nil, ConnectError(err)
	}
	engine, err := s.d.Durable.Bind(tenant)
	if err != nil {
		return nil, ConnectError(err)
	}
	status, state, err := s.status(ctx, engine, row)
	if err != nil {
		return nil, ConnectError(err)
	}
	return connect.NewResponse(&taskapiv1.QueryTaskResponse{
		TaskId:      row.TaskID,
		SessionId:   row.SessionID,
		Status:      status,
		RoundSeq:    state.GetRoundSeq(),
		TaskState:   state.GetTaskState(),
		LastEventId: row.LastEventID,
	}), nil
}

func terminal(st commonv1.TaskStatus) bool {
	switch st {
	case commonv1.TaskStatus_TASK_STATUS_DONE, commonv1.TaskStatus_TASK_STATUS_ABORTED,
		commonv1.TaskStatus_TASK_STATUS_CANCELLED, commonv1.TaskStatus_TASK_STATUS_FAILED:
		return true
	}
	return false
}

// status 给出 Task 的状态与工作流内的 Task State：
// 记下终态的以记录为准；子工作流还没启动的是排队中；其余读子工作流的 task_state 查询。
func (s *Server) status(ctx context.Context, engine durable.Engine, row taskstore.Task) (commonv1.TaskStatus, *workflowv1.TaskStateQueryResult, error) {
	if terminal(row.Status) {
		state := &workflowv1.TaskStateQueryResult{}
		if err := engine.Query(ctx, row.TaskID, session.QueryTaskState, state); err != nil {
			// 排队中被取消的 Task 没有子工作流；查询失败时只返回记下的终态。
			return row.Status, nil, nil
		}
		return row.Status, state, nil
	}
	desc, err := engine.Describe(ctx, row.TaskID)
	if providererr.CategoryOf(err) == providererr.NotFound {
		return commonv1.TaskStatus_TASK_STATUS_QUEUED, nil, nil
	}
	if err != nil {
		return commonv1.TaskStatus_TASK_STATUS_UNSPECIFIED, nil, err
	}
	state := &workflowv1.TaskStateQueryResult{}
	if err := engine.Query(ctx, row.TaskID, session.QueryTaskState, state); err == nil && state.GetStatus() != commonv1.TaskStatus_TASK_STATUS_UNSPECIFIED {
		return state.GetStatus(), state, nil
	}
	switch desc.Status {
	case durable.StatusRunning:
		return commonv1.TaskStatus_TASK_STATUS_RUNNING, nil, nil
	case durable.StatusCancelled:
		return commonv1.TaskStatus_TASK_STATUS_CANCELLED, nil, nil
	case durable.StatusCompleted:
		return commonv1.TaskStatus_TASK_STATUS_DONE, nil, nil
	default:
		return commonv1.TaskStatus_TASK_STATUS_FAILED, nil, nil
	}
}

// ErrorDomain 是 ConnectError 附带的 google.rpc.ErrorInfo 的 domain。
const ErrorDomain = "agentruntime"

// ConnectError 把统一错误模型映射为 connect 错误码：invalid → InvalidArgument，denied → PermissionDenied
// （凭据问题为 Unauthenticated），not_found → NotFound，conflict → AlreadyExists，transient → Unavailable
// （限流为 ResourceExhausted，并带 Retry-After）。错误信息里不带凭据。错误模型的 code 放进 google.rpc.ErrorInfo
// （domain 为 ErrorDomain）。
func ConnectError(err error) *connect.Error {
	var ce *connect.Error
	if errors.As(err, &ce) {
		return ce
	}
	var pe *providererr.Error
	if !errors.As(err, &pe) {
		return connect.NewError(connect.CodeInternal, errors.New("内部错误"))
	}
	code := connect.CodeInternal
	switch pe.Category {
	case providererr.Invalid:
		code = connect.CodeInvalidArgument
	case providererr.Denied:
		code = connect.CodePermissionDenied
		if pe.Code == "unauthenticated" {
			code = connect.CodeUnauthenticated
		}
	case providererr.NotFound:
		code = connect.CodeNotFound
	case providererr.Conflict:
		code = connect.CodeAlreadyExists
	case providererr.Transient:
		code = connect.CodeUnavailable
		if pe.Code == "rate_limit" {
			code = connect.CodeResourceExhausted
		}
	}
	// 只带错误模型里的说明，不带底层原因：原因里可能有 SQL、内部地址之类的细节。
	out := connect.NewError(code, errors.New(pe.Message))
	// 错误模型里稳定的 code（例如 network_not_allowed）放进标准的 google.rpc.ErrorInfo：说明是中文，应用按 code
	// 翻译成自己界面的语言；HTTP + JSON 的调用方在 details[].debug.reason 里读到它。
	if pe.Code != "" {
		if detail, err := connect.NewErrorDetail(&errdetails.ErrorInfo{Reason: pe.Code, Domain: ErrorDomain}); err == nil {
			out.AddDetail(detail)
		}
	}
	if pe.RetryAfter > 0 {
		out.Meta().Set("Retry-After", strconv.Itoa(int(math.Ceil(pe.RetryAfter.Seconds()))))
	}
	return out
}
