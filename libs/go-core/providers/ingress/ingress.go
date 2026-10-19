// Package ingress 定义 Ingress 接口（设计书 4.14）：认证、租户识别、限流。
//
// 只在 Go 侧（Task Service 的入口）使用。网关产品只定义接口；开发实现是内置的令牌表。
// 协议：控制指令走 HTTP POST，进度与结果走 SSE，断线后凭 Last-Event-ID 从 Cache 的推流续传（见 providers/cache）。
//
// Ingress 是唯一不按租户绑定的 Provider：它负责从认证身份推出租户。语义：
//   - Authenticate：凭据缺失、无效、被篡改或过期：Denied。返回的身份里 TenantID 合乎格式。
//   - Admit：按身份所属租户限流；超过上限返回 Transient（code = rate_limit），带 RetryAfter。租户之间互不影响。
//   - TenantContextFor：由认证身份构造租户上下文。请求里的 tenant_id 只用来做一致性校验，非空且不一致时 Denied
//     （设计书 4.1：调用方无法绕过租户隔离）。
package ingress

import (
	"context"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Credentials 是请求携带的凭据。
type Credentials struct {
	BearerToken string
}

// Identity 是认证后的身份。
type Identity struct {
	TenantID  string
	UserID    string
	Scopes    []string
	ExpiresAt time.Time
}

// Ingress 是入口能力。
type Ingress interface {
	Authenticate(ctx context.Context, creds Credentials) (Identity, error)
	// Admit 判断这个身份此刻能否再发起一个请求；route 是限流的维度，例如 submit_task。
	Admit(ctx context.Context, id Identity, route string) error
	Close() error
}

// TenantContextFor 由认证身份构造租户上下文，并校验请求里的字段。
func TenantContextFor(id Identity, requestTenantID, workspaceID, sessionID string, trace *commonv1.TraceContext) (*commonv1.TenantContext, error) {
	if id.TenantID == "" {
		return nil, providererr.New(providererr.Denied, "unauthenticated", "没有认证身份")
	}
	if requestTenantID != "" && requestTenantID != id.TenantID {
		return nil, providererr.New(providererr.Denied, "tenant_mismatch", "请求的租户与认证身份不一致")
	}
	ctx := &commonv1.TenantContext{
		TenantId:    id.TenantID,
		UserId:      id.UserID,
		WorkspaceId: workspaceID,
		SessionId:   sessionID,
		Trace:       trace,
	}
	scope, err := tenancy.TenantScope(ctx)
	if err != nil {
		return nil, err
	}
	return scope.TenantContext()
}
