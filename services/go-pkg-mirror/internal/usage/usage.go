// Package usage 记每个租户每天从上游拉的字节数（包镜像的未命中，设计书改动稿 W7、W10）。命中不计。
//
// 计数在内存里累加，定期（Flush）写进业务库里那个租户的 sandbox_usage（mirror_miss_bytes 列；表由 Worker 迁移，D-11），
// Worker 建环境时按它核对租户策略的 daily_mirror_miss_bytes。第一次见到某个租户的某一天时，先从业务库读回已有的数。
// 业务库暂时写不进（表还没建、连不上）时数留在内存里，下次再写。
package usage

import (
	"context"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Recorder 是包镜像用到的计数接口。
type Recorder interface {
	// Used 是这个租户今天（UTC）已经从上游拉了多少字节。
	Used(ctx context.Context, tenant string, now time.Time) int64
	// Add 加上这次从上游拉的字节数。
	Add(tenant string, n int64, now time.Time)
}

type key struct{ tenant, day string }

type counter struct {
	base    int64 // 业务库里已有的
	pending int64 // 还没写进去的
	loaded  bool
}

// Tracker 是带业务库的计数（store 为 nil 时只在内存里，测试与开发用）。
type Tracker struct {
	store relational.Provider
	mu    sync.Mutex
	c     map[key]*counter
}

// New 建一个计数器。
func New(store relational.Provider) *Tracker {
	return &Tracker{store: store, c: map[key]*counter{}}
}

func day(now time.Time) string { return now.UTC().Format("2006-01-02") }

func (t *Tracker) get(ctx context.Context, tenant string, now time.Time) *counter {
	k := key{tenant, day(now)}
	t.mu.Lock()
	c := t.c[k]
	if c == nil {
		c = &counter{}
		t.c[k] = c
	}
	loaded := c.loaded
	t.mu.Unlock()
	if !loaded && t.store != nil {
		if base, err := t.read(ctx, tenant, k.day); err == nil {
			t.mu.Lock()
			if !c.loaded {
				c.base, c.loaded = base, true
			}
			t.mu.Unlock()
		}
	}
	return c
}

// Used 见 Recorder。
func (t *Tracker) Used(ctx context.Context, tenant string, now time.Time) int64 {
	c := t.get(ctx, tenant, now)
	t.mu.Lock()
	defer t.mu.Unlock()
	return c.base + c.pending
}

// Add 见 Recorder。
func (t *Tracker) Add(tenant string, n int64, now time.Time) {
	if n <= 0 {
		return
	}
	k := key{tenant, day(now)}
	t.mu.Lock()
	defer t.mu.Unlock()
	c := t.c[k]
	if c == nil {
		c = &counter{}
		t.c[k] = c
	}
	c.pending += n
}

// Snapshot 是内存里每个租户今天的数（指标用）。
func (t *Tracker) Snapshot(now time.Time) map[string]int64 {
	today := day(now)
	t.mu.Lock()
	defer t.mu.Unlock()
	out := map[string]int64{}
	for k, c := range t.c {
		if k.day == today {
			out[k.tenant] = c.base + c.pending
		}
	}
	return out
}

// Flush 把还没写进业务库的数写进去；返回写不进的个数。旧的天数写完后丢掉。
func (t *Tracker) Flush(ctx context.Context, now time.Time) int {
	if t.store == nil {
		return 0
	}
	t.mu.Lock()
	type job struct {
		k key
		n int64
	}
	var jobs []job
	for k, c := range t.c {
		if c.pending > 0 {
			jobs = append(jobs, job{k, c.pending})
		}
	}
	t.mu.Unlock()
	failed := 0
	for _, j := range jobs {
		if err := t.write(ctx, j.k.tenant, j.k.day, j.n); err != nil {
			failed++
			continue
		}
		t.mu.Lock()
		if c := t.c[j.k]; c != nil {
			c.pending -= j.n
			c.base += j.n
		}
		t.mu.Unlock()
	}
	today := day(now)
	t.mu.Lock()
	for k, c := range t.c {
		if k.day != today && c.pending == 0 {
			delete(t.c, k)
		}
	}
	t.mu.Unlock()
	return failed
}

func (t *Tracker) bind(tenant string) (relational.Store, error) {
	if tenant == "_platform" {
		return t.store.BindPlatform("pkg-mirror")
	}
	return t.store.Bind(&commonv1.TenantContext{TenantId: tenant})
}

func (t *Tracker) read(ctx context.Context, tenant, day string) (int64, error) {
	store, err := t.bind(tenant)
	if err != nil {
		return 0, err
	}
	var n int64
	err = store.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, "SELECT mirror_miss_bytes FROM sandbox_usage WHERE day = $1", day)
		if err != nil || !ok {
			return err
		}
		if v, ok := row["mirror_miss_bytes"].(int64); ok {
			n = v
		}
		return nil
	})
	return n, err
}

func (t *Tracker) write(ctx context.Context, tenant, day string, n int64) error {
	if tenant == "_platform" {
		return nil // 平台作用域不计入任何租户
	}
	store, err := t.bind(tenant)
	if err != nil {
		return err
	}
	return store.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx,
			"INSERT INTO sandbox_usage (day, mirror_miss_bytes) VALUES ($1, $2) "+
				"ON CONFLICT (day) DO UPDATE SET mirror_miss_bytes = sandbox_usage.mirror_miss_bytes + excluded.mirror_miss_bytes",
			day, n)
		return err
	})
}
