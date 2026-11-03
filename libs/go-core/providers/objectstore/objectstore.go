// Package objectstore 定义 ObjectStore 接口（设计书 4.7）：按租户前缀读写对象、临时访问地址、生命周期策略。
//
// 两种语言共用：Python 侧见 libs/py-core/src/agentruntime_core/providers/object_store.py，方法一一对应，语义一致：
//   - 键：1 到 1024 个字符，用 / 分段；不得以 / 开头，不得有空段、. 或 .. 段、反斜杠与控制字符，否则 Invalid。
//   - 对象的引用是 obj://<tenant_id>/<键>，平台作用域为 obj://_platform/<键>。KeyOf 解析引用，
//     引用属于别的作用域时报 Denied，格式不对报 Invalid。
//   - Put 同一个键、同样内容可以重复执行；IfAbsent 且键已存在时报 Conflict。
//   - Get、Head 不存在的键：NotFound。Delete 返回删除前是否存在，重复删除不报错。
//   - TemporaryURL 生成限时地址，过期后不可用；ttl 必须大于 0。
//   - SignedUpload 生成只能写一次、只能写指定内容的上传地址（STOR-SEC，docs/plan/workspace-read-latency-and-disk-2026-11-03.md
//     第 8.2 节）：用 PUT，原样带上返回的 Headers；内容的 sha256 与长度必须与签的一致，对象已存在时拒绝（不覆盖）。地址交给不受信任
//     的一方（沙箱里的 agent，也就是用户代码）也无害。sha256 是 64 位小写十六进制，size 不能为负，ttl 在 0 与 MaxSignedUploadTTL
//     之间，否则 Invalid。
//   - SetLifecycle 替换本作用域的生命周期规则：前缀下创建超过 ExpireAfter 的对象被清理。
//     对象的生命周期必须长于 Temporal 的 retention（计划 S-06），由部署配置保证。
package objectstore

import (
	"context"
	"regexp"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// MaxSignedUploadTTL 是 SignedUpload 的地址最长有效多久：按块分批签，每批几分钟。
const MaxSignedUploadTTL = 15 * time.Minute

var sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)

// SignedUpload 是 Store.SignedUpload 的结果：用 PUT 上传，原样带上 Headers（它们都在签名里，少一个、改一个都被拒）。
type SignedUpload struct {
	URL     string
	Headers map[string]string
}

// CheckSignedUpload 校验 SignedUpload 的参数，各实现共用。
func CheckSignedUpload(sha256 string, size int64, ttl time.Duration) error {
	if !sha256Hex.MatchString(sha256) {
		return providererr.New(providererr.Invalid, "invalid_argument", "sha256 要是 64 位小写十六进制")
	}
	if size < 0 {
		return providererr.New(providererr.Invalid, "invalid_argument", "size 不能为负")
	}
	if ttl <= 0 || ttl > MaxSignedUploadTTL {
		return providererr.New(providererr.Invalid, "invalid_argument", "ttl 要大于 0、不超过 900 秒")
	}
	return nil
}

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
	// SignedUpload 生成只能写一次、只能写 sha256 与 size 指定的内容的 PUT 地址。
	SignedUpload(ctx context.Context, key string, ttl time.Duration, sha256 string, size int64) (SignedUpload, error)
	SetLifecycle(ctx context.Context, rules []LifecycleRule) error
}
