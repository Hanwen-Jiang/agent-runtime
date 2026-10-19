// Package durable 定义 DurableEngine 接口（设计书 4.2、附录 K）：启动与取消工作流、信号与查询。
//
// 两种语言共用：Python 侧见 libs/py-core/src/agentruntime_core/providers/durable_engine.py，方法一一对应，语义一致：
//   - 输入、信号载荷、查询结果与工作流结果都是 protobuf 消息，编码为 json/protobuf（计划 S-01）。
//   - 工作流 ID：1 到 255 个可打印字符，否则 Invalid。实现在工作流上记录所属作用域，每次访问都核对。
//   - StartWorkflow 幂等：同一 ID 的工作流正在运行时返回已有的运行，AlreadyStarted 为 true；类型不同报 Conflict。
//     ID 属于别的作用域：Conflict。
//   - Signal、Query、Describe、Result 访问不存在或属于别的作用域的工作流：NotFound。
//   - Cancel 幂等：已取消或已结束再取消不报错；不存在或属于别的作用域：NotFound。
//   - Result 等到工作流结束，返回结局；等待超过 timeout：Transient。
//
// 这里只是客户端操作。工作流代码、activity、定时器与版本分支直接用 Temporal SDK 编写，不经过本接口。
// 开发实现是 Temporal 本地开发服务。
package durable

import (
	"context"
	"time"

	"google.golang.org/protobuf/proto"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Provider 是长期存在的工厂。
type Provider interface {
	Bind(tenant *commonv1.TenantContext) (Engine, error)
	BindPlatform(purpose string) (Engine, error)
	Close() error
}

// Status 是工作流的状态。
type Status string

const (
	StatusRunning        Status = "running"
	StatusCompleted      Status = "completed"
	StatusFailed         Status = "failed"
	StatusCancelled      Status = "cancelled"
	StatusTerminated     Status = "terminated"
	StatusContinuedAsNew Status = "continued_as_new"
	StatusTimedOut       Status = "timed_out"
)

// Run 是一次启动的结果。
type Run struct {
	WorkflowID     string
	RunID          string
	AlreadyStarted bool
}

// Description 是工作流的概况。CloseTime 为零值表示还在运行。
type Description struct {
	WorkflowID   string
	RunID        string
	WorkflowType string
	TaskQueue    string
	Status       Status
	StartTime    time.Time
	CloseTime    time.Time
}

// Outcome 是工作流的结局。Status 为 Completed 时结果已写入调用方传入的消息。
type Outcome struct {
	Status Status
	// Failure 是失败、取消或超时的说明，不含凭据。
	Failure string
}

// StartOptions 是启动参数。
type StartOptions struct {
	WorkflowID       string
	TaskQueue        string
	Input            proto.Message
	ExecutionTimeout time.Duration
}

// Engine 是绑定了作用域的客户端。
type Engine interface {
	StartWorkflow(ctx context.Context, workflowType string, opts StartOptions) (Run, error)
	// SignalWithStart 工作流不存在就启动，再投递信号。
	SignalWithStart(ctx context.Context, workflowType string, opts StartOptions, signal string, payload proto.Message) (Run, error)
	Signal(ctx context.Context, workflowID, signal string, payload proto.Message) error
	// Query 把查询结果写入 result。
	Query(ctx context.Context, workflowID, query string, result proto.Message) error
	Cancel(ctx context.Context, workflowID, reason string) error
	Describe(ctx context.Context, workflowID string) (Description, error)
	// Result 等工作流结束；Completed 时把结果写入 result。timeout 为 0 表示一直等。
	Result(ctx context.Context, workflowID string, result proto.Message, timeout time.Duration) (Outcome, error)
}
