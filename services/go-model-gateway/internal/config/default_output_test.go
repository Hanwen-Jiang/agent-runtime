package config

// BUDGET-DEFAULT：AR_MODEL_DEFAULT_MAX_OUTPUT 是请求没写输出上限时的可见输出上限。不设时为 0（网关的默认值）；0 表示不设上限
// （按 Profile 的 max_output，之前的做法），记成 -1；正整数照用；别的写法报错（只提变量名）。

import "testing"

func TestDefaultMaxOutput(t *testing.T) {
	for raw, want := range map[string]int{"": 0, "0": -1, "8192": 8192, " 32768 ": 32768} {
		m := base()
		m[EnvDefaultMaxOutput] = raw
		cfg, err := FromEnv(env(m))
		if err != nil || cfg.DefaultMaxOutput != want {
			t.Errorf("%q：%d %v，want %d", raw, cfg.DefaultMaxOutput, err, want)
		}
	}
	for _, raw := range []string{"-5", "many", "1.5"} {
		m := base()
		m[EnvDefaultMaxOutput] = raw
		if _, err := FromEnv(env(m)); err == nil || err.Error() != EnvDefaultMaxOutput+" 要是不小于 0 的整数（0 表示不设上限）" {
			t.Errorf("%q 应报错：%v", raw, err)
		}
	}
}
