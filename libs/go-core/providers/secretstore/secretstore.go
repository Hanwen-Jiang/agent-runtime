// Package secretstore 定义 SecretStore 接口（设计书 4.12）：读取密钥、按租户签发短期动态凭据、吊销，以及数据密钥。
//
// 两种语言共用：Python 侧见 libs/py-core/src/agentruntime_core/providers/secret_store.py，方法一一对应，语义一致：
//   - Read：读取本作用域的静态密钥（平台作用域为基础设施密钥，租户作用域为该租户的密钥，例如 BYOK）。
//     不存在或属于别的作用域：NotFound。
//   - IssueCredential：按角色签发短期凭据。角色没有授予本作用域：Denied。ttl 必须大于 0 且不超过上限，否则 Invalid。
//   - LeaseStatus：Active、Revoked 或 Expired。租约不存在或属于别的作用域：NotFound。
//   - Revoke：吊销后立即变为 Revoked；重复吊销不报错；不存在或属于别的作用域：NotFound。
//   - GenerateDataKey：生成 32 字节数据密钥，返回明文与包装后的密文（信封加密），供 Claim-Check 与向量加密使用。
//   - UnwrapDataKey：密文属于别的作用域或 purpose 不同：Denied；密文损坏：Invalid。
//
// 节点代码永远拿不到长期密钥。所有明文都是 secret.Value，错误文本里不出现明文。
package secretstore

import (
	"context"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Provider 是长期存在的工厂。
type Provider interface {
	Bind(tenant *commonv1.TenantContext) (Store, error)
	BindPlatform(purpose string) (Store, error)
	Close() error
}

// LeaseStatus 是短期凭据的状态。
type LeaseStatus string

const (
	LeaseActive  LeaseStatus = "active"
	LeaseRevoked LeaseStatus = "revoked"
	LeaseExpired LeaseStatus = "expired"
)

// Credential 是一份短期凭据。
type Credential struct {
	LeaseID   string
	Role      string
	Secret    secret.Value
	ExpiresAt time.Time
}

// DataKey 是一把数据密钥。
type DataKey struct {
	KeyID     string
	Plaintext secret.Value
	Wrapped   []byte
}

// Store 是绑定了作用域的客户端。
type Store interface {
	Read(ctx context.Context, name string) (secret.Value, error)
	IssueCredential(ctx context.Context, role string, ttl time.Duration) (Credential, error)
	LeaseStatus(ctx context.Context, leaseID string) (LeaseStatus, error)
	Revoke(ctx context.Context, leaseID string) error
	GenerateDataKey(ctx context.Context, purpose string) (DataKey, error)
	UnwrapDataKey(ctx context.Context, wrapped []byte, purpose string) (secret.Value, error)
}

// Writer 是可写的 Store（只在 Go 侧，不属于两种语言共用的接口）：用户自己的网络密钥（SEC-API）由 task-service 写进租户作用域，
// 出网网关按名字现读。绑定作用域的 Store 实现了它才能写；不实现的（例如只读的生产实现）调用方按不可用处理。
//   - Write：没有同名的新建，有则替换；之后的 Read 读到新值（别的进程也一样）。
//   - Delete：删掉本作用域的这个名字，立即生效；不存在时返回 false，不报错。
//
// 值是 secret.Value，错误文本与日志里不出现。
type Writer interface {
	Write(ctx context.Context, name string, value secret.Value) error
	Delete(ctx context.Context, name string) (bool, error)
}
