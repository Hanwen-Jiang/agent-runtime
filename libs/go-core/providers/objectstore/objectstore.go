// Package objectstore 定义 ObjectStore 接口（设计书 4.7）：按租户前缀读写对象、临时访问地址、生命周期策略。
//
// 两种语言共用：Python 侧见 libs/py-core/src/agentruntime_core/providers/object_store.py，方法一一对应，语义一致：
//   - 键：1 到 1024 个字符，用 / 分段；不得以 / 开头，不得有空段、. 或 .. 段、反斜杠与控制字符，否则 Invalid。
//   - 对象的引用是 obj://<tenant_id>/<键>，平台作用域为 obj://_platform/<键>。KeyOf 解析引用，
//     引用属于别的作用域时报 Denied，格式不对报 Invalid。
//   - Put 同一个键、同样内容可以重复执行；IfAbsent 且键已存在时报 Conflict。
//   - Get、Head 不存在的键：NotFound。Delete 返回删除前是否存在，重复删除不报错。
//   - TemporaryURL 生成限时地址，过期后不可用；ttl 必须大于 0。
//   - SetLifecycle 替换本作用域的生命周期规则：前缀下创建超过 ExpireAfter 的对象被清理。
//     对象的生命周期必须长于 Temporal 的 retention（计划 S-06），由部署配置保证。
package objectstore

import (
	"context"
	"time"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Provider 是长期存在的工厂。
type Provider interface {
	Bind(tenant *commonv1.TenantContext) (Store, error)
	BindPlatform(purpose string) (Store, error)
	Close() error
}

// ObjectInfo 是对象的元数据。
type ObjectInfo struct {
	Key         string
	URI         string
	Size        int64
	SHA256      string
	ContentType string
	CreatedAt   time.Time
	Metadata    map[string]string
}

// ObjectData 是对象内容与元数据。
type ObjectData struct {
	Info ObjectInfo
	Data []byte
}

// ObjectPage 是 List 的一页结果；NextCursor 为空表示没有下一页。
type ObjectPage struct {
	Items      []ObjectInfo
	NextCursor string
}

// LifecycleRule 是一条生命周期规则。
type LifecycleRule struct {
	Prefix      string
	ExpireAfter time.Duration
}

// PutOptions 是 Put 的可选参数；ContentType 为空时按 application/octet-stream。
type PutOptions struct {
	ContentType string
	Metadata    map[string]string
	IfAbsent    bool
}

// ListOptions 是 List 的参数；Limit 为 0 时按 1000。
type ListOptions struct {
	Prefix string
	Limit  int
	Cursor string
}

// Store 是绑定了作用域的客户端。
type Store interface {
	URI(key string) string
	KeyOf(uri string) (string, error)
	Put(ctx context.Context, key string, data []byte, opts PutOptions) (ObjectInfo, error)
	Get(ctx context.Context, key string) (ObjectData, error)
	Head(ctx context.Context, key string) (ObjectInfo, error)
	Delete(ctx context.Context, key string) (bool, error)
	List(ctx context.Context, opts ListOptions) (ObjectPage, error)
	// TemporaryURL 生成限时地址；method 为 GET 或 PUT。
	TemporaryURL(ctx context.Context, key string, ttl time.Duration, method string) (string, error)
	SetLifecycle(ctx context.Context, rules []LifecycleRule) error
}
