// Package cache 定义 Cache 接口（设计书 4.5）：带过期的键值、计数与限流、分布式锁、推流。
//
// 两种语言共用：Python 侧见 libs/py-core/src/agentruntime_core/providers/cache.py，方法一一对应，语义一致：
//   - 键、锁名、流名：1 到 512 个字符，不含空白与控制字符，否则 Invalid。每个作用域各有一套命名空间。
//   - ttl、window、retention 必须大于 0，否则 Invalid。所有写入都必须带过期时间。
//   - Incr：键不存在时从 0 开始，并在创建时设 ttl；之后的 Incr 不改 ttl。
//   - RateLimit：滑动窗口，任意 window 时长内累计 cost 不超过 limit；被拒绝的请求不计入。
//   - 锁：同一作用域内同名锁同一时刻只有一个持有者；过期后别人可以拿到；过期的持有者续期、释放都失败。
//   - 推流：同一个流里 AppendEvent 返回的 id 从 1 开始严格递增；超过 retention 的事件被清理，
//     ReadEvents 发现 after 之后有事件已被清理时 Gap 为 true；Subscribe 先补发已有事件，再推送新事件。
//
// 只存可丢失或可重建的数据。
package cache

import (
	"context"
	"time"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Provider 是长期存在的工厂。
type Provider interface {
	Bind(tenant *commonv1.TenantContext) (Cache, error)
	BindPlatform(purpose string) (Cache, error)
	Close() error
}

// RateDecision 是限流结果。
type RateDecision struct {
	Allowed    bool
	Remaining  int64
	RetryAfter time.Duration
}

// Lock 是一把分布式锁。
type Lock struct {
	Name      string
	Token     string
	ExpiresAt time.Time
}

// StreamEvent 是推流里的一个事件。
type StreamEvent struct {
	ID   int64
	Data []byte
}

// EventPage 是 ReadEvents 的结果。
type EventPage struct {
	Events []StreamEvent
	// Gap 为 true 表示 after 之后有事件已过保留期被清理。
	Gap bool
}

// Subscription 依次返回事件；Close 后 Next 返回错误。
type Subscription interface {
	Next(ctx context.Context) (StreamEvent, error)
	Close() error
}

// Cache 是绑定了作用域的客户端。
type Cache interface {
	// Get 返回值；键不存在时 ok 为 false。
	Get(ctx context.Context, key string) (value []byte, ok bool, err error)
	Set(ctx context.Context, key string, value []byte, ttl time.Duration) error
	// SetIfAbsent 键不存在时写入并返回 true；已存在时不改值，返回 false。
	SetIfAbsent(ctx context.Context, key string, value []byte, ttl time.Duration) (bool, error)
	// Delete 返回删除前键是否存在。
	Delete(ctx context.Context, key string) (bool, error)
	Incr(ctx context.Context, key string, by int64, ttl time.Duration) (int64, error)
	RateLimit(ctx context.Context, key string, limit int64, window time.Duration, cost int64) (RateDecision, error)
	// AcquireLock 拿到锁返回 Lock；已被别人持有返回 nil。
	AcquireLock(ctx context.Context, name string, ttl time.Duration) (*Lock, error)
	// ExtendLock 续期成功返回新的 Lock；锁已过期或易主返回 nil。
	ExtendLock(ctx context.Context, lock Lock, ttl time.Duration) (*Lock, error)
	// ReleaseLock 释放成功返回 true；锁已过期或易主返回 false。
	ReleaseLock(ctx context.Context, lock Lock) (bool, error)
	AppendEvent(ctx context.Context, stream string, data []byte, retention time.Duration) (int64, error)
	ReadEvents(ctx context.Context, stream string, after int64, limit int) (EventPage, error)
	Subscribe(ctx context.Context, stream string, after int64) (Subscription, error)
}
