// Package temporaladapter 是 DurableEngine 的 Temporal 适配器。语义见 providers/durable。
//
// 开发与生产用同一份代码，只是连的 Temporal 服务不同。作用域记在工作流的 memo 里（agentruntime_scope），
// 每次访问先 describe 核对；不一致一律按不存在处理。Python 侧见 agentruntime_core.adapters.temporal，
// memo 键与取值相同，两种语言启动的工作流可以互相访问。
package temporaladapter

import (
	"context"
	"errors"
	"reflect"
	"regexp"
	"time"

	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/temporal"
	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// ScopeMemoKey 是记录作用域的 memo 键。
const ScopeMemoKey = "agentruntime_scope"

// ScopeMemo 返回租户作用域的 memo。工作流自己启动子工作流时用它，子工作流才能经绑定同一租户的 Engine 访问。
// 纯函数，工作流代码可以调用。
func ScopeMemo(tenant *commonv1.TenantContext) (map[string]any, error) {
	s, err := tenancy.TenantScope(tenant)
	if err != nil {
		return nil, err
	}
	return map[string]any{ScopeMemoKey: devutil.ScopeID(s)}, nil
}

var workflowID = regexp.MustCompile(`^[^\x00-\x1f\x7f]{1,255}$`)

var statuses = map[enumspb.WorkflowExecutionStatus]durable.Status{
	enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING:          durable.StatusRunning,
	enumspb.WORKFLOW_EXECUTION_STATUS_COMPLETED:        durable.StatusCompleted,
	enumspb.WORKFLOW_EXECUTION_STATUS_FAILED:           durable.StatusFailed,
	enumspb.WORKFLOW_EXECUTION_STATUS_CANCELED:         durable.StatusCancelled,
	enumspb.WORKFLOW_EXECUTION_STATUS_TERMINATED:       durable.StatusTerminated,
	enumspb.WORKFLOW_EXECUTION_STATUS_CONTINUED_AS_NEW: durable.StatusContinuedAsNew,
	enumspb.WORKFLOW_EXECUTION_STATUS_TIMED_OUT:        durable.StatusTimedOut,
}

// MapError 把 Temporal 的服务端错误映射到统一的错误模型。
func MapError(err error) error {
	var notFound *serviceerror.NotFound
	var invalid *serviceerror.InvalidArgument
	var started *serviceerror.WorkflowExecutionAlreadyStarted
	var exists *serviceerror.AlreadyExists
	var denied *serviceerror.PermissionDenied
	switch {
	case errors.As(err, &notFound):
		return providererr.Wrap(providererr.NotFound, "not_found", "工作流不存在", err)
	case errors.As(err, &invalid):
		return providererr.Wrap(providererr.Invalid, "invalid_argument", "请求不合法", err)
	case errors.As(err, &started), errors.As(err, &exists):
		return providererr.Wrap(providererr.Conflict, "already_exists", "工作流已存在", err)
	case errors.As(err, &denied):
		return providererr.Wrap(providererr.Denied, "permission_denied", "没有权限访问 Temporal", err)
	default:
		return providererr.Wrap(providererr.Transient, "unavailable", "Temporal 暂时不可用", err)
	}
}

type provider struct{ c client.Client }

// New 构造 DurableEngine 的 Temporal 适配器。
func New(c client.Client) durable.Provider { return provider{c: c} }

func (p provider) Bind(t *commonv1.TenantContext) (durable.Engine, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &engine{c: p.c, owner: devutil.ScopeID(s)}, nil
}

func (p provider) BindPlatform(purpose string) (durable.Engine, error) {
	s, err := tenancy.PlatformScope(purpose)
	if err != nil {
		return nil, err
	}
	return &engine{c: p.c, owner: devutil.ScopeID(s)}, nil
}

func (p provider) Close() error { return nil }

type engine struct {
	c     client.Client
	owner string
}

func checkID(id string) error {
	if !workflowID.MatchString(id) {
		return providererr.New(providererr.Invalid, "invalid_workflow_id", "工作流 ID 必须是 1 到 255 个可打印字符")
	}
	return nil
}

// describe 返回工作流的描述；不存在时返回 nil。不核对作用域。
func (e *engine) describe(ctx context.Context, id string) (*workflowservice.DescribeWorkflowExecutionResponse, string, error) {
	resp, err := e.c.DescribeWorkflowExecution(ctx, id, "")
	if err != nil {
		var notFound *serviceerror.NotFound
		if errors.As(err, &notFound) {
			return nil, "", nil
		}
		return nil, "", MapError(err)
	}
	owner := ""
	if payload := resp.GetWorkflowExecutionInfo().GetMemo().GetFields()[ScopeMemoKey]; payload != nil {
		_ = converter.GetDefaultDataConverter().FromPayload(payload, &owner)
	}
	return resp, owner, nil
}

func (e *engine) owned(ctx context.Context, id string) (*workflowservice.DescribeWorkflowExecutionResponse, error) {
	if err := checkID(id); err != nil {
		return nil, err
	}
	resp, owner, err := e.describe(ctx, id)
	if err != nil {
		return nil, err
	}
	if resp == nil || owner != e.owner {
		return nil, providererr.New(providererr.NotFound, "not_found", "工作流不存在")
	}
	return resp, nil
}

func (e *engine) existingRun(ctx context.Context, workflowType, id string) (*durable.Run, error) {
	resp, owner, err := e.describe(ctx, id)
	if err != nil || resp == nil {
		return nil, err
	}
	if owner != e.owner {
		return nil, providererr.New(providererr.Conflict, "id_in_use", "工作流 ID 已被别的作用域使用")
	}
	info := resp.GetWorkflowExecutionInfo()
	if info.GetStatus() != enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING {
		return nil, nil
	}
	if info.GetType().GetName() != workflowType {
		return nil, providererr.New(providererr.Conflict, "type_mismatch", "同一 ID 的工作流类型不同")
	}
	return &durable.Run{WorkflowID: id, RunID: info.GetExecution().GetRunId(), AlreadyStarted: true}, nil
}

func args(input proto.Message) []any {
	if input == nil || reflect.ValueOf(input).IsNil() {
		return nil
	}
	return []any{input}
}

func (e *engine) options(opts durable.StartOptions) client.StartWorkflowOptions {
	return client.StartWorkflowOptions{
		ID:                                       opts.WorkflowID,
		TaskQueue:                                opts.TaskQueue,
		WorkflowExecutionTimeout:                 opts.ExecutionTimeout,
		Memo:                                     map[string]any{ScopeMemoKey: e.owner},
		WorkflowIDConflictPolicy:                 enumspb.WORKFLOW_ID_CONFLICT_POLICY_FAIL,
		WorkflowExecutionErrorWhenAlreadyStarted: true,
	}
}

func (e *engine) StartWorkflow(ctx context.Context, workflowType string, opts durable.StartOptions) (durable.Run, error) {
	if err := checkID(opts.WorkflowID); err != nil {
		return durable.Run{}, err
	}
	if run, err := e.existingRun(ctx, workflowType, opts.WorkflowID); err != nil || run != nil {
		return derefRun(run), err
	}
	run, err := e.c.ExecuteWorkflow(ctx, e.options(opts), workflowType, args(opts.Input)...)
	if err != nil {
		var started *serviceerror.WorkflowExecutionAlreadyStarted
		if errors.As(err, &started) {
			if run, err2 := e.existingRun(ctx, workflowType, opts.WorkflowID); err2 != nil || run != nil {
				return derefRun(run), err2
			}
		}
		return durable.Run{}, MapError(err)
	}
	return durable.Run{WorkflowID: opts.WorkflowID, RunID: run.GetRunID()}, nil
}

func derefRun(r *durable.Run) durable.Run {
	if r == nil {
		return durable.Run{}
	}
	return *r
}

func (e *engine) SignalWithStart(ctx context.Context, workflowType string, opts durable.StartOptions, signal string, payload proto.Message) (durable.Run, error) {
	if err := checkID(opts.WorkflowID); err != nil {
		return durable.Run{}, err
	}
	existing, err := e.existingRun(ctx, workflowType, opts.WorkflowID)
	if err != nil {
		return durable.Run{}, err
	}
	startOpts := e.options(opts)
	startOpts.WorkflowIDConflictPolicy = enumspb.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING
	startOpts.WorkflowExecutionErrorWhenAlreadyStarted = false
	var arg any
	if payload != nil && !reflect.ValueOf(payload).IsNil() {
		arg = payload
	}
	run, err := e.c.SignalWithStartWorkflow(ctx, opts.WorkflowID, signal, arg, startOpts, workflowType, args(opts.Input)...)
	if err != nil {
		return durable.Run{}, MapError(err)
	}
	if existing != nil {
		return *existing, nil
	}
	return durable.Run{WorkflowID: opts.WorkflowID, RunID: run.GetRunID()}, nil
}

func (e *engine) Signal(ctx context.Context, id, signal string, payload proto.Message) error {
	if _, err := e.owned(ctx, id); err != nil {
		return err
	}
	var arg any
	if payload != nil && !reflect.ValueOf(payload).IsNil() {
		arg = payload
	}
	if err := e.c.SignalWorkflow(ctx, id, "", signal, arg); err != nil {
		return MapError(err)
	}
	return nil
}

// decodeInto 用 get 解码出一个与 result 同类型的新消息，再写入 result。
func decodeInto(result proto.Message, get func(any) error) error {
	target := reflect.New(reflect.TypeOf(result))
	if err := get(target.Interface()); err != nil {
		return err
	}
	decoded, _ := target.Elem().Interface().(proto.Message)
	proto.Reset(result)
	if decoded != nil {
		proto.Merge(result, decoded)
	}
	return nil
}

func (e *engine) Query(ctx context.Context, id, query string, result proto.Message) error {
	if _, err := e.owned(ctx, id); err != nil {
		return err
	}
	value, err := e.c.QueryWorkflow(ctx, id, "", query)
	if err != nil {
		return MapError(err)
	}
	if err := decodeInto(result, value.Get); err != nil {
		return providererr.Wrap(providererr.Invalid, "invalid_result", "查询结果的类型不对", err)
	}
	return nil
}

func (e *engine) Cancel(ctx context.Context, id, _ string) error {
	resp, err := e.owned(ctx, id)
	if err != nil {
		return err
	}
	if resp.GetWorkflowExecutionInfo().GetStatus() != enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING {
		return nil
	}
	if err := e.c.CancelWorkflow(ctx, id, ""); err != nil {
		var notFound *serviceerror.NotFound
		if !errors.As(err, &notFound) {
			return MapError(err)
		}
	}
	return nil
}

func (e *engine) Describe(ctx context.Context, id string) (durable.Description, error) {
	resp, err := e.owned(ctx, id)
	if err != nil {
		return durable.Description{}, err
	}
	info := resp.GetWorkflowExecutionInfo()
	d := durable.Description{
		WorkflowID:   info.GetExecution().GetWorkflowId(),
		RunID:        info.GetExecution().GetRunId(),
		WorkflowType: info.GetType().GetName(),
		TaskQueue:    info.GetTaskQueue(),
		Status:       statuses[info.GetStatus()],
		StartTime:    info.GetStartTime().AsTime(),
	}
	if info.GetCloseTime() != nil {
		d.CloseTime = info.GetCloseTime().AsTime()
	}
	return d, nil
}

func (e *engine) Result(ctx context.Context, id string, result proto.Message, timeout time.Duration) (durable.Outcome, error) {
	if _, err := e.owned(ctx, id); err != nil {
		return durable.Outcome{}, err
	}
	waitCtx := ctx
	if timeout > 0 {
		var cancel context.CancelFunc
		waitCtx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	run := e.c.GetWorkflow(waitCtx, id, "")
	err := decodeInto(result, func(ptr any) error { return run.Get(waitCtx, ptr) })
	if err == nil {
		return durable.Outcome{Status: durable.StatusCompleted}, nil
	}
	var canceled *temporal.CanceledError
	var terminated *temporal.TerminatedError
	var timedOut *temporal.TimeoutError
	var app *temporal.ApplicationError
	switch {
	case errors.As(err, &canceled):
		return durable.Outcome{Status: durable.StatusCancelled, Failure: canceled.Error()}, nil
	case errors.As(err, &terminated):
		return durable.Outcome{Status: durable.StatusTerminated, Failure: terminated.Error()}, nil
	case errors.As(err, &timedOut):
		return durable.Outcome{Status: durable.StatusTimedOut, Failure: timedOut.Error()}, nil
	case errors.As(err, &app):
		return durable.Outcome{Status: durable.StatusFailed, Failure: app.Message()}, nil
	case errors.Is(err, context.DeadlineExceeded) || waitCtx.Err() != nil:
		return durable.Outcome{}, providererr.Wrap(providererr.Transient, "timeout", "等待工作流结束超时", err)
	default:
		return durable.Outcome{}, MapError(err)
	}
}
