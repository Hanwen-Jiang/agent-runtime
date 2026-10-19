// Package clock 提供时钟接口。有过期语义的开发实现都从这里取时间，测试可以换成 testkit.FakeClock。
//
// 注意：Temporal 工作流代码必须用 workflow.Now，不能用它。
package clock

import (
	"context"
	"time"
)

// Clock 返回 UTC 时间，并能按它的时间等待。
type Clock interface {
	Now() time.Time
	Sleep(ctx context.Context, d time.Duration) error
}

// System 是真实时钟。
type System struct{}

func (System) Now() time.Time { return time.Now().UTC() }

func (System) Sleep(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
