// Package tenancy 是租户绑定（计划 S-02 难点“租户上下文的传法”：绑定在按请求创建的客户端上，全仓统一）。
//
// Python 与 Go 共用（Python 侧见 libs/py-core/src/agentruntime_core/tenancy.py），两边语义一致：
//   - 每个 Provider 都是一个长期存在的工厂，按请求调用 Bind(tenant) 得到绑定了租户的客户端；
//     之后在客户端上的调用不再传租户，也无法改成别的租户。
//   - Bind 时复制租户上下文；调用方事后修改原消息，不影响已绑定的客户端。
//   - 缺少租户上下文、tenant_id 为空：Denied。tenant_id 等字段格式不对：Invalid。
//   - 平台自有的数据用 BindPlatform(purpose) 访问，purpose 必填。平台作用域与任何租户作用域互不可见。
//   - 跨租户读取不在本期接口内，由 S-12 用单独的角色并留审计。
package tenancy

import (
	"regexp"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

var (
	// 与 /schemas/defs.schema.json 的 tenant_id、workspace_id、user_id 一致。
	slug = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
	// 与 proto IdKind 的 ses 前缀一致。
	sessionID = regexp.MustCompile(`^ses_[0-7][0-9A-HJKMNP-TV-Z]{25}$`)
)

// Kind 区分租户作用域与平台作用域。
type Kind int

const (
	KindTenant Kind = iota + 1
	KindPlatform
)

// Scope 是一个已绑定客户端的作用域，值类型，构造后不可变。
type Scope struct {
	Kind        Kind
	TenantID    string
	UserID      string
	WorkspaceID string
	SessionID   string
	Traceparent string
	Tracestate  string
	// Purpose 是平台作用域的用途说明。
	Purpose string
}

// IsPlatform 判断是否为平台作用域。
func (s Scope) IsPlatform() bool { return s.Kind == KindPlatform }

// TenantContext 转回 proto，供跨服务调用携带。平台作用域没有租户上下文。
func (s Scope) TenantContext() (*commonv1.TenantContext, error) {
	if s.IsPlatform() {
		return nil, providererr.New(providererr.Denied, "platform_scope", "平台作用域没有租户上下文")
	}
	t := &commonv1.TenantContext{
		TenantId:    s.TenantID,
		UserId:      s.UserID,
		WorkspaceId: s.WorkspaceID,
		SessionId:   s.SessionID,
	}
	// 没有 trace 上下文时不带空的 TraceContext，与原始输入保持一致。
	if s.Traceparent != "" || s.Tracestate != "" {
		t.Trace = &commonv1.TraceContext{Traceparent: s.Traceparent, Tracestate: s.Tracestate}
	}
	return t, nil
}

// TenantScope 校验并复制租户上下文。所有 Provider 实现的 Bind 都调用它。
func TenantScope(tenant *commonv1.TenantContext) (Scope, error) {
	if tenant == nil || tenant.GetTenantId() == "" {
		return Scope{}, providererr.New(providererr.Denied, "missing_tenant", "缺少租户上下文")
	}
	if !slug.MatchString(tenant.GetTenantId()) {
		return Scope{}, providererr.New(providererr.Invalid, "malformed_tenant", "tenant_id 格式不对")
	}
	for name, value := range map[string]string{"user_id": tenant.GetUserId(), "workspace_id": tenant.GetWorkspaceId()} {
		if value != "" && !slug.MatchString(value) {
			return Scope{}, providererr.New(providererr.Invalid, "malformed_tenant", name+" 格式不对")
		}
	}
	if s := tenant.GetSessionId(); s != "" && !sessionID.MatchString(s) {
		return Scope{}, providererr.New(providererr.Invalid, "malformed_tenant", "session_id 格式不对")
	}
	return Scope{
		Kind:        KindTenant,
		TenantID:    tenant.GetTenantId(),
		UserID:      tenant.GetUserId(),
		WorkspaceID: tenant.GetWorkspaceId(),
		SessionID:   tenant.GetSessionId(),
		Traceparent: tenant.GetTrace().GetTraceparent(),
		Tracestate:  tenant.GetTrace().GetTracestate(),
	}, nil
}

// PlatformScope 构造平台作用域。所有 Provider 实现的 BindPlatform 都调用它。
func PlatformScope(purpose string) (Scope, error) {
	purpose = strings.TrimSpace(purpose)
	if purpose == "" {
		return Scope{}, providererr.New(providererr.Invalid, "missing_purpose", "平台作用域必须写明用途")
	}
	return Scope{Kind: KindPlatform, Purpose: purpose}, nil
}
