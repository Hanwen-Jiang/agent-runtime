// Package testkit 是开发实现与一致性测试共用的测试支持：可拨动的时钟与故障注入。
//
// 开发实现在构造时接收 Clock 与 Faults，一致性测试的 harness 用它们拨动时间、注入错误。生产实现不需要它们。
package testkit

import (
	"context"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

// FakeClock 只在 Advance 时前进；Sleep 等到时钟被拨过目标时间才返回。
type FakeClock struct {
	mu      sync.Mutex
	now     time.Time
	waiters []waiter
}

type waiter struct {
	at   time.Time
	done chan struct{}
}

// NewFakeClock 从 2026-10-01T00:00:00Z 开始。
func NewFakeClock() *FakeClock {
	return &FakeClock{now: time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)}
}

func (c *FakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *FakeClock) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
	}
	c.mu.Lock()
	w := waiter{at: c.now.Add(d), done: make(chan struct{})}
	c.waiters = append(c.waiters, w)
	c.mu.Unlock()
	select {
	case <-w.done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Advance 让时钟前进 d，并唤醒到期的 Sleep。d 不能为负。
func (c *FakeClock) Advance(d time.Duration) {
	if d < 0 {
		panic("testkit: clock cannot go back")
	}
	c.mu.Lock()
	c.now = c.now.Add(d)
	var keep []waiter
	for _, w := range c.waiters {
		if !w.at.After(c.now) {
			close(w.done)
		} else {
			keep = append(keep, w)
		}
	}
	c.waiters = keep
	c.mu.Unlock()
}

// Faults 按操作名注入错误。开发实现在每个公开方法开头调用 Check(操作名)。
type Faults struct {
	mu      sync.Mutex
	pending map[string][]*providererr.Error
}

// NewFaults 构造一个空的故障注入器。
func NewFaults() *Faults { return &Faults{pending: map[string][]*providererr.Error{}} }

// FailNext 让下一次 operation 返回该类别的错误。
func (f *Faults) FailNext(operation string, category providererr.Category) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.pending[operation] = append(f.pending[operation], providererr.New(category, "injected", "注入的故障："+operation))
}

// Check 若有为 operation 安排的错误，取出并返回它。
func (f *Faults) Check(operation string) error {
	if f == nil {
		return nil
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	queue := f.pending[operation]
	if len(queue) == 0 {
		return nil
	}
	f.pending[operation] = queue[1:]
	return queue[0]
}
