package gateway_test

// TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 9.3 节：网关的计量也经 Telemetry 发成指标
// （Collector 写进 VictoriaMetrics，Grafana 画趋势与告警）。细化见该计划第 11.9 节：
//   - 每次调用（Generate、StreamGenerate）一次 runtime.model.calls 计数：outcome（ok、error）、error.type（失败时的 code）、
//     gen_ai.provider.name、gen_ai.response.model（失败且没有选中模型时为空）、role、tier；租户由绑定的 Telemetry 加上。
//   - 成功且计费时：runtime.model.cost_usd（美元）、gen_ai.client.token.usage（gen_ai.token.type 为 input、output 各一次）、
//     gen_ai.client.operation.duration（秒）三个观测。
//   - 属性里没有消息与输出原文。

import (
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/consoletelemetry"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func metricsNamed(tel *consoletelemetry.Provider, name string) []consoletelemetry.MetricRecord {
	var out []consoletelemetry.MetricRecord
	flushed(tel)
	for _, m := range tel.Metrics() {
		if m.Name == name {
			out = append(out, m)
		}
	}
	return out
}

func TestASuccessfulCallIsCountedAndMetered(t *testing.T) {
	h, tel, _ := withTelemetry(t)
	h.fake.setUsage(&commonv1.Usage{InputTokens: 2000, OutputTokens: 1000})
	c := call(commonv1.Role_ROLE_TRANSFORM)
	c.Tier = "mid@low"
	h.mustGenerate(t, "t1", c, say(secretPrompt))

	calls := metricsNamed(tel, "runtime.model.calls")
	if len(calls) != 1 || calls[0].Value != 1 {
		t.Fatalf("一次调用一次计数：%+v", calls)
	}
	want := map[string]any{
		"outcome": "ok", "gen_ai.provider.name": "deepseek", "gen_ai.response.model": "mid-a", "role": "transform", "tier": "mid@low", "tenant_id": "t1",
	}
	for k, v := range want {
		if calls[0].Attributes[k] != v {
			t.Errorf("runtime.model.calls 的 %s = %v，want %v", k, calls[0].Attributes[k], v)
		}
	}
	spans := chatSpans(tel)
	cost := metricsNamed(tel, "runtime.model.cost_usd")
	if len(cost) != 1 || len(spans) != 1 || cost[0].Value != spans[0].Attributes["runtime.cost_usd"] {
		t.Errorf("成本与 span 上记的一致：%+v", cost)
	}
	tokens := map[any]float64{}
	for _, m := range metricsNamed(tel, "gen_ai.client.token.usage") {
		tokens[m.Attributes["gen_ai.token.type"]] = m.Value
	}
	if tokens["input"] != 2000 || tokens["output"] != 1000 {
		t.Errorf("token 按 input、output 各记一次：%v", tokens)
	}
	if d := metricsNamed(tel, "gen_ai.client.operation.duration"); len(d) != 1 || d[0].Value < 0 {
		t.Errorf("每次调用记一次用时（秒）：%+v", d)
	}
	for _, m := range tel.Metrics() {
		for _, v := range m.Attributes {
			if s, ok := v.(string); ok && strings.Contains(s, secretPrompt) {
				t.Errorf("指标 %s 带了消息原文", m.Name)
			}
		}
	}
}

func TestAFailedCallIsCountedWithItsCodeAndNotMetered(t *testing.T) {
	h, tel, _ := withTelemetry(t)
	h.fake.failNext("mid-a", transient("timeout", 0), transient("timeout", 0), transient("timeout", 0))
	h.fake.failNext("mid-b", transient("timeout", 0), transient("timeout", 0), transient("rate_limit", time.Second))
	if _, err := h.generate(t, "t1", call(transform), say("hi")); err == nil {
		t.Fatal("同档的模型都失败，调用应失败")
	}
	calls := metricsNamed(tel, "runtime.model.calls")
	if len(calls) != 1 || calls[0].Attributes["outcome"] != "error" || calls[0].Attributes["error.type"] != "models_exhausted" {
		t.Fatalf("失败的调用记 outcome=error 与 code：%+v", calls)
	}
	if cost := metricsNamed(tel, "runtime.model.cost_usd"); len(cost) != 0 {
		t.Errorf("没有计费就不记成本：%+v", cost)
	}
}
