package session

// S-03 验收：上限（计划 S-03 难点“上限”，附录 K.3）。未完成的子工作流、activity、信号各默认 2,000，
// Session 工作流对并行的只读 Task 设上限；事件历史在 10,240 个事件告警之前续接。

import (
	"testing"
	"time"
)

func TestDefaultConfigIsValidAndWellBelowTemporalLimits(t *testing.T) {
	cfg := DefaultConfig()
	if err := cfg.Validate(); err != nil {
		t.Fatalf("默认参数应当合法：%v", err)
	}
	if cfg.ReadOnlyLimit < 1 || cfg.ReadOnlyLimit > 1000 {
		t.Errorf("只读 Task 上限应在 1 到 1,000 之间，给写 Task 与 activity 留出余量，实际 %d", cfg.ReadOnlyLimit)
	}
	if cfg.ContinueAsNewEvents < 1 || cfg.ContinueAsNewEvents >= 10240 {
		t.Errorf("续接阈值应低于 10,240 个事件的告警线，实际 %d", cfg.ContinueAsNewEvents)
	}
	if cfg.ContinueAsNewTasks != 0 {
		t.Errorf("默认不按 Task 数续接（这是测试用的开关），实际 %d", cfg.ContinueAsNewTasks)
	}
	if cfg.DedupeLimit < 1 {
		t.Errorf("去重表上限必须大于 0，实际 %d", cfg.DedupeLimit)
	}
	if cfg.ActivityTimeout <= 0 {
		t.Errorf("activity 超时必须大于 0，实际 %v", cfg.ActivityTimeout)
	}
}

func TestConfigValidateRejectsOutOfRangeValues(t *testing.T) {
	cases := map[string]func(*Config){
		"只读上限为 0":          func(c *Config) { c.ReadOnlyLimit = 0 },
		"只读上限达到子工作流上限": func(c *Config) { c.ReadOnlyLimit = 2000 },
		"续接事件数为 0":         func(c *Config) { c.ContinueAsNewEvents = 0 },
		"续接事件数超过告警线":    func(c *Config) { c.ContinueAsNewEvents = 20000 },
		"续接 Task 数为负":      func(c *Config) { c.ContinueAsNewTasks = -1 },
		"去重表上限为 0":         func(c *Config) { c.DedupeLimit = 0 },
		"去重表过大，续接输入会超载荷上限": func(c *Config) { c.DedupeLimit = 1_000_000 },
		"activity 超时为 0":    func(c *Config) { c.ActivityTimeout = 0 },
		"activity 超时为负":    func(c *Config) { c.ActivityTimeout = -time.Second },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			cfg := DefaultConfig()
			mutate(&cfg)
			if err := cfg.Validate(); err == nil {
				t.Errorf("应当拒绝：%+v", cfg)
			}
		})
	}
}
