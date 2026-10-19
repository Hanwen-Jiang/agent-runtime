package session

import (
	"fmt"
	"time"
)

// Config 是 Session 工作流的参数。工作流启动时用 SideEffect 记下一份，之后只用记下的值，
// 因此重放不受当前进程配置的影响；新参数在下一次 Continue-As-New 后生效。
type Config struct {
	// ReadOnlyLimit 是同时运行的只读 Task 上限，超出的排队。
	ReadOnlyLimit int
	// ContinueAsNewEvents：事件历史达到这个长度就续接（附录 K.3 的告警线是 10,240）。
	ContinueAsNewEvents int
	// ContinueAsNewTasks：本次运行接受的 Task 数达到这个值就续接；0 表示不按 Task 数续接。
	ContinueAsNewTasks int
	// DedupeLimit 是记住的 client_request_id 个数上限；只淘汰已结束的 Task。
	DedupeLimit int
	// ActivityTimeout 是 AcceptTask 与 FinishTask 的 start-to-close 超时。
	ActivityTimeout time.Duration
}

// 上限：未完成的子工作流、activity、信号各默认 2,000（附录 K.3），只读 Task 再加上写 Task 与 activity 要留余量；
// 事件历史 10,240 个事件告警；续接输入里的去重表受单个载荷 2 MB 的限制。
const (
	maxReadOnlyLimit       = 1000
	maxContinueAsNewEvents = 10000
	maxDedupeLimit         = 10000
)

// DefaultConfig 返回默认参数。
func DefaultConfig() Config {
	return Config{
		ReadOnlyLimit:       8,
		ContinueAsNewEvents: 4000,
		DedupeLimit:         1000,
		ActivityTimeout:     30 * time.Second,
	}
}

// Validate 校验参数，并保证各项远低于附录 K.3 的上限。
func (c Config) Validate() error {
	switch {
	case c.ReadOnlyLimit < 1 || c.ReadOnlyLimit > maxReadOnlyLimit:
		return fmt.Errorf("只读 Task 并行上限必须在 1 到 %d 之间，实际 %d", maxReadOnlyLimit, c.ReadOnlyLimit)
	case c.ContinueAsNewEvents < 1 || c.ContinueAsNewEvents > maxContinueAsNewEvents:
		return fmt.Errorf("续接事件数必须在 1 到 %d 之间，实际 %d", maxContinueAsNewEvents, c.ContinueAsNewEvents)
	case c.ContinueAsNewTasks < 0:
		return fmt.Errorf("续接 Task 数不能为负，实际 %d", c.ContinueAsNewTasks)
	case c.DedupeLimit < 1 || c.DedupeLimit > maxDedupeLimit:
		return fmt.Errorf("去重表上限必须在 1 到 %d 之间，实际 %d", maxDedupeLimit, c.DedupeLimit)
	case c.ActivityTimeout <= 0:
		return fmt.Errorf("activity 超时必须大于 0，实际 %v", c.ActivityTimeout)
	}
	return nil
}
