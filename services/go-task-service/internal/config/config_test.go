package config

// S-03 验收：task-service 的配置。Session 工作流的上限经这里设置，必须过 session.Config.Validate
// （计划 S-03 难点“上限”）；签名密钥不得出现在日志与错误信息里（CLAUDE.md“其他”）。

import (
	"fmt"
	"strings"
	"testing"
)

const key = "dev-signing-key-should-never-be-printed"

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func base() map[string]string {
	return map[string]string{
		EnvTemporalAddress: "127.0.0.1:7233",
		EnvHTTPAddr:        "127.0.0.1:8080",
		EnvPublicBaseURL:   "http://127.0.0.1:8080",
		EnvDevSigningKey:   key,
		EnvDataDir:         "data",
	}
}

func TestFromEnvDefaults(t *testing.T) {
	cfg, err := FromEnv(env(base()))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.TemporalNamespace != "default" {
		t.Errorf("默认 namespace 为 default，实际 %q", cfg.TemporalNamespace)
	}
	if cfg.DevSigningKey.RevealString() != key {
		t.Error("签名密钥没有读进来")
	}
	if err := cfg.Session.Validate(); err != nil {
		t.Errorf("默认的 Session 参数应当合法：%v", err)
	}
}

func TestFromEnvSessionLimits(t *testing.T) {
	m := base()
	m[EnvReadOnlyLimit] = "2"
	m[EnvContinueAsNewTasks] = "3"
	m[EnvContinueAsNewEvents] = "4000"
	cfg, err := FromEnv(env(m))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Session.ReadOnlyLimit != 2 || cfg.Session.ContinueAsNewTasks != 3 || cfg.Session.ContinueAsNewEvents != 4000 {
		t.Errorf("Session 参数没有读进来：%+v", cfg.Session)
	}
}

func TestFromEnvRejectsBadValues(t *testing.T) {
	cases := map[string]map[string]string{
		"缺少 Temporal 地址": {EnvTemporalAddress: ""},
		"缺少签名密钥":        {EnvDevSigningKey: ""},
		"只读上限不是数字":      {EnvReadOnlyLimit: "many"},
		"只读上限超出范围":      {EnvReadOnlyLimit: "5000"},
		"续接事件数超出范围":     {EnvContinueAsNewEvents: "50000"},
		"订阅地址不是 URL":     {EnvPublicBaseURL: "not a url"},
	}
	for name, override := range cases {
		t.Run(name, func(t *testing.T) {
			m := base()
			for k, v := range override {
				m[k] = v
			}
			_, err := FromEnv(env(m))
			if err == nil {
				t.Fatal("应当报错")
			}
			if strings.Contains(err.Error(), key) {
				t.Error("错误信息不能带出签名密钥")
			}
		})
	}
}

func TestConfigNeverPrintsTheSigningKey(t *testing.T) {
	cfg, err := FromEnv(env(base()))
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{fmt.Sprint(cfg), fmt.Sprintf("%+v", cfg), fmt.Sprintf("%#v", cfg)} {
		if strings.Contains(s, key) {
			t.Fatalf("打印配置时泄露了签名密钥：%s", s)
		}
	}
}
