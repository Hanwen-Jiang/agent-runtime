package gateway_test

// T-15：Model Gateway 的模型调用 span（设计书 4.13、附录 J.1；细化见 docs/plan/deploy-telemetry.md 第 2 节第 2 条）。
// 每次 Generate、StreamGenerate 一个 `chat {模型}` span，按 GenAI 语义约定带模型与 token，另带 tier、成本、task_id、node_id；
// span 绑定在请求的租户上；消息与输出原文、密钥一律不出现。网关不在每次 RPC 后 flush（会抵消 OpenTelemetry 的批量导出），测试自己 flush。

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/consoletelemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"

	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
)

const secretPrompt = "PROMPT-TEXT-must-not-be-traced"

func withTelemetry(t *testing.T) (*harness, *consoletelemetry.Provider, *bytes.Buffer) {
	t.Helper()
	out := &bytes.Buffer{}
	tel := consoletelemetry.New(consoletelemetry.Options{Writer: out})
	t.Cleanup(func() { _ = tel.Close() })
	h := newHarnessOpts(t, nil, func(o *gateway.Options) { o.Telemetry = tel })
	return h, tel, out
}

// flushed 让控制台实现把缓冲里的记录导出（控制台实现的 flush 作用于整个 Provider）。
func flushed(tel *consoletelemetry.Provider) *consoletelemetry.Provider {
	c, err := tel.BindPlatform("test")
	if err == nil {
		_ = c.Flush(context.Background())
	}
	return tel
}

func chatSpans(tel *consoletelemetry.Provider) []consoletelemetry.SpanRecord {
	var out []consoletelemetry.SpanRecord
	flushed(tel)
	for _, s := range tel.Spans() {
		if strings.HasPrefix(s.Name, "chat") {
			out = append(out, s)
		}
	}
	return out
}

func TestGenerateRecordsAChatSpanWithGenAIAttributes(t *testing.T) {
	h, tel, _ := withTelemetry(t)
	h.fake.setUsage(&commonv1.Usage{InputTokens: 2000, OutputTokens: 1000})
	c := call(commonv1.Role_ROLE_TRANSFORM)
	c.Tier = "mid@low"
	h.mustGenerate(t, "t1", c, say(secretPrompt))

	spans := chatSpans(tel)
	if len(spans) != 1 {
		t.Fatalf("一次 Generate 一个 chat span，得到 %d 个", len(spans))
	}
	s := spans[0]
	if s.Name != "chat mid-a" {
		t.Errorf("span 名应为 `chat {模型}`，得到 %q", s.Name)
	}
	want := map[string]any{
		"gen_ai.operation.name":      "chat",
		"gen_ai.provider.name":       "deepseek",
		"gen_ai.request.model":       "mid-a",
		"gen_ai.response.model":      "mid-a",
		"gen_ai.usage.input_tokens":  int64(2000),
		"gen_ai.usage.output_tokens": int64(1000),
		"tier":                       "mid@low",
		"task_id":                    "task-1",
		"node_id":                    "node-1",
		"role":                       "transform",
		"tenant_id":                  "t1",
	}
	for k, v := range want {
		if got := s.Attributes[k]; got != v {
			t.Errorf("属性 %s 应为 %v（%T），得到 %v（%T）", k, v, v, got, got)
		}
	}
	// 2000*0.15 + 1000*0.6 = 900 微美元。
	if got := s.Attributes["runtime.cost_usd"]; got != 0.0009 {
		t.Errorf("runtime.cost_usd 应为 0.0009，得到 %v", got)
	}
	if s.Status != "ok" {
		t.Errorf("成功的调用 span 状态应为 ok，得到 %s", s.Status)
	}
}

func TestSwitchedCallsRecordTheModelThatServed(t *testing.T) {
	h, tel, _ := withTelemetry(t)
	h.fake.failNext("mid-a", transient("timeout", 0), transient("timeout", 0), transient("timeout", 0))
	c := call(commonv1.Role_ROLE_TRANSFORM)
	c.Tier = "mid@low"
	h.mustGenerate(t, "t1", c, say("hi"))
	spans := chatSpans(tel)
	if len(spans) != 1 {
		t.Fatalf("同档切换仍是一次调用一个 span，得到 %d 个", len(spans))
	}
	if got := spans[0].Attributes["gen_ai.response.model"]; got != "mid-b" {
		t.Errorf("gen_ai.response.model 应是实际服务的模型 mid-b，得到 %v", got)
	}
	if got := spans[0].Attributes["runtime.attempts"]; got != int64(4) {
		t.Errorf("runtime.attempts 应记下尝试次数 4，得到 %v", got)
	}
}

func TestFailedCallsRecordAnErrorSpanWithTheCode(t *testing.T) {
	h, tel, _ := withTelemetry(t)
	h.fake.failNext("mid-a", providererr.New(providererr.Invalid, "content_filtered", "内容被拦截"))
	c := call(commonv1.Role_ROLE_TRANSFORM)
	c.Tier = "mid@low"
	if _, err := h.generate(t, "t1", c, say(secretPrompt)); err == nil {
		t.Fatal("应失败")
	}
	spans := chatSpans(tel)
	if len(spans) != 1 {
		t.Fatalf("失败的调用也记一个 span，得到 %d 个", len(spans))
	}
	if spans[0].Status != "error" || spans[0].Attributes["error.type"] != "content_filtered" {
		t.Errorf("失败的 span 应为 error，error.type 为 code：%+v", spans[0])
	}
}

func TestStreamGenerateRecordsOneSpan(t *testing.T) {
	h, tel, _ := withTelemetry(t)
	c := call(commonv1.Role_ROLE_DELIVER)
	c.Tier = "mid@low"
	if _, err := h.streamAll(t, &mg.StreamGenerateRequest{Tenant: tenant("t1"), Call: c, Generation: say("hi")}); err != nil {
		t.Fatal(err)
	}
	spans := chatSpans(tel)
	if len(spans) != 1 || spans[0].Attributes["gen_ai.operation.name"] != "chat" {
		t.Fatalf("流式调用一个 chat span：%+v", spans)
	}
	if spans[0].Attributes["runtime.stream"] != true {
		t.Errorf("流式调用标 runtime.stream=true")
	}
}

func TestSpansNeverCarryPromptsOutputsOrKeys(t *testing.T) {
	h, tel, out := withTelemetry(t)
	c := call(commonv1.Role_ROLE_TRANSFORM)
	c.Tier = "mid@low"
	h.mustGenerate(t, "t1", c, say(secretPrompt+" "+leakyKey))
	dump, _ := json.Marshal(flushed(tel).Spans())
	for _, leaked := range []string{secretPrompt, leakyKey} {
		if strings.Contains(string(dump), leaked) || strings.Contains(out.String(), leaked) {
			t.Errorf("span 里出现了 %q", leaked)
		}
	}
}

func TestSpansAreBoundToTheRequestTenant(t *testing.T) {
	h, tel, _ := withTelemetry(t)
	c := call(commonv1.Role_ROLE_TRANSFORM)
	c.Tier = "mid@low"
	h.mustGenerate(t, "t1", c, say("a"))
	h.mustGenerate(t, "t2", c, say("b"))
	got := map[any]int{}
	for _, s := range chatSpans(tel) {
		got[s.Attributes["tenant_id"]]++
	}
	if got["t1"] != 1 || got["t2"] != 1 {
		t.Fatalf("每个 span 绑定在请求的租户上：%v", got)
	}
}

func TestWithoutTelemetryTheGatewayStillWorks(t *testing.T) {
	h := newHarness(t) // Options.Telemetry 为 nil：丢弃
	c := call(commonv1.Role_ROLE_TRANSFORM)
	c.Tier = "mid@low"
	h.mustGenerate(t, "t1", c, say("hi"))
}
