// Package telemetry 定义 Telemetry 接口（设计书 4.13、附录 J.1）：span、指标与日志的打点与导出。
//
// 两种语言共用：Python 侧见 libs/py-core/src/agentruntime_core/providers/telemetry.py，方法一一对应，语义一致：
//   - 绑定租户的客户端自动给每条记录加上 tenant_id、user_id、workspace_id、session_id；调用方传入同名属性一律忽略。
//     平台作用域加 scope=platform，不带租户属性。
//   - 租户上下文带有 traceparent 时，根 span 接在它下面（trace_id 相同）。
//   - 值为 secret.Value 的属性、键名含 password、secret、token、api_key、apikey、authorization、credential、cookie
//     的属性，一律记为 [REDACTED]。
//   - Span.RecordError 把 span 记为 error 并记录错误类型。
//   - 导出失败不影响业务调用：StartSpan、Count、Observe、Log 只在参数不合法（例如名称为空）时返回 Invalid；
//     导出失败只体现在 Flush，且只能是 Transient。
//
// Python 用 async with start_span(...) 自动结束 span；Go 由调用方 defer span.End()。
package telemetry

import (
	"context"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Redacted 是脱敏后的属性值。
const Redacted = "[REDACTED]"

// Provider 是长期存在的工厂。
type Provider interface {
	Bind(tenant *commonv1.TenantContext) (Telemetry, error)
	BindPlatform(purpose string) (Telemetry, error)
	Close() error
}

// Attribute 是一个属性。Value 可以是 string、bool、int64、float64、secret.Value 或它们的切片。
type Attribute struct {
	Key   string
	Value any
}

// Level 是日志级别。
type Level string

const (
	LevelDebug   Level = "debug"
	LevelInfo    Level = "info"
	LevelWarning Level = "warning"
	LevelError   Level = "error"
)

// Span 是一个进行中的 span。
type Span interface {
	TraceID() string
	SpanID() string
	SetAttributes(attrs ...Attribute)
	AddEvent(name string, attrs ...Attribute)
	RecordError(err error)
	End()
}

// Telemetry 是绑定了作用域的客户端。
type Telemetry interface {
	// StartSpan 开始一个 span，返回携带它的 ctx；ctx 里已有 span 时作为其子 span。名称为空时返回 Invalid。
	StartSpan(ctx context.Context, name string, attrs ...Attribute) (context.Context, Span, error)
	Count(ctx context.Context, name string, value int64, attrs ...Attribute) error
	Observe(ctx context.Context, name string, value float64, attrs ...Attribute) error
	Log(ctx context.Context, level Level, message string, attrs ...Attribute) error
	Flush(ctx context.Context) error
}
