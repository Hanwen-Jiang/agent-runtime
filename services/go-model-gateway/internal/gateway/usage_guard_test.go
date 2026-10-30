package gateway_test

// TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 11.11 节：SWE-bench 冒烟与中转核实查出的网关问题。
//   - 回答里有请求没给的工具调用（没给工具时任何工具调用都算）：这次尝试记一行 failed（error_code unexpected_tool_call，带上已计费的
//     用量），不在同一个 Profile 上重试，换同档的下一个；同档都这样时报 Invalid unexpected_tool_call（不报 transient）。
//   - 这次 RPC 里已计费的花费回给调用方：成功时回答的 Usage 是所有已计费尝试之和；失败时错误的 details 带 billed_cost_usd_micros。
//   - 有回答而 input、output token 都是 0：照常返回；计量记 usage_missing、写一条警告日志、记计数 runtime.model.usage_missing。

import (
	"encoding/json"
	"strings"
	"testing"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
)

// billedCostDetail 是 common.v1.Error.details 里的字段名（只加字段，不改契约；Python 客户端读同一个名字）。
const billedCostDetail = "billed_cost_usd_micros"

// 假供应商默认 Usage 的成本（见 fakes_test.go）：mid-a 435；mid-b (1000-200)*0.3 + 200*0.03 + 500*2.5 = 1496。
const (
	midACost = 435
	midBCost = 1496
)

// callsTool 让模型回一个名为 name 的工具调用（中转塞进来的 Bash 之类），Usage 用假供应商的默认值。
func callsTool(name string) generateFunc {
	return func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		return modelprovider.GenerateResult{
			ToolCalls:    []modelprovider.ToolCall{{ID: "call-x", Name: name, Arguments: json.RawMessage(`{"command":"ls"}`)}},
			FinishReason: modelprovider.FinishToolCalls,
		}, nil
	}
}

func withSearchTool(t *testing.T, text string) *mg.GenerateParams {
	t.Helper()
	g := say(text)
	g.Tools = []*mg.ToolSpec{{Name: "search", ParamsSchema: &commonv1.JsonPayload{Value: jsonValue(t, `{"type":"object"}`)}}}
	return g
}

func billedMicros(pe *commonv1.Error) (int64, bool) {
	v, ok := pe.GetDetails().GetFields()[billedCostDetail]
	if !ok {
		return 0, false
	}
	return int64(v.GetNumberValue()), true
}

func TestAToolCallOutsideTheOfferedToolsMovesToTheNextProfile(t *testing.T) {
	h := newHarness(t)
	h.fake.onGenerate("mid-a", callsTool("Bash"))
	resp := h.mustGenerate(t, "t1", call(commonv1.Role_ROLE_ACT), withSearchTool(t, "找一下"))
	if resp.GetModel().GetModelProfileId() != "test/mid-b@1" || resp.GetText() != "reply from mid-b" || len(resp.GetToolCalls()) != 0 {
		t.Fatalf("没给的工具调用不交给调用方，换同档的下一个 Profile：%v", resp)
	}
	if n := h.fake.callsTo("mid-a"); n != 1 {
		t.Errorf("不在同一个 Profile 上重试：mid-a 调了 %d 次", n)
	}
	lines := h.meterLines(t)
	if len(lines) != 2 {
		t.Fatalf("两次尝试各一行计量：%s", h.meter.String())
	}
	if lines[0]["status"] != "failed" || lines[0]["error_code"] != "unexpected_tool_call" || lines[0]["model_profile_id"] != "test/mid-a@1" ||
		lines[0]["cost_usd_micros"] != float64(midACost) {
		t.Errorf("没给的工具调用记一行已计费的失败：%v", lines[0])
	}
	if lines[1]["status"] != "ok" || lines[1]["cost_usd_micros"] != float64(midBCost) {
		t.Errorf("成功的那次照常记，成本只算它自己：%v", lines[1])
	}
	u := resp.GetUsage()
	if u.GetCostUsdMicros() != midACost+midBCost || u.GetInputTokens() != 2000 || u.GetOutputTokens() != 1000 {
		t.Errorf("回答的 Usage 是这次 RPC 里所有已计费尝试之和：%v", u)
	}
	if strings.Contains(h.meter.String(), "command") {
		t.Error("计量记录不含工具调用的参数")
	}
}

func TestAnyToolCallIsUnexpectedWhenNoToolsWereOffered(t *testing.T) {
	h := newHarness(t)
	// 中转把结构化输出变成工具调用（2026-10-30 的核实：finish 是 tool_calls、没有正文）。
	h.fake.onGenerate("mid-a", callsTool("output"))
	h.fake.onGenerate("mid-b", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		out := json.RawMessage(`{"answer":"2"}`)
		return modelprovider.GenerateResult{Text: string(out), Structured: out}, nil
	})
	g := say("1+1?")
	g.OutputSchema = &commonv1.JsonPayload{Value: jsonValue(t, answerSchema)}
	resp := h.mustGenerate(t, "t1", call(transform), g)
	if resp.GetModel().GetModelProfileId() != "test/mid-b@1" || resp.GetResult().GetStructValue().GetFields()["answer"].GetStringValue() != "2" {
		t.Fatalf("要结构化输出却回了工具调用：换下一个 Profile：%v", resp)
	}
	if lines := h.meterLines(t); len(lines) != 2 || lines[0]["error_code"] != "unexpected_tool_call" {
		t.Errorf("第一次记 unexpected_tool_call：%v", lines)
	}
}

func TestUnexpectedToolCallsOnEveryProfileAreInvalidAndCarryTheBilledCost(t *testing.T) {
	h := newHarness(t)
	h.fake.onGenerate("mid-a", callsTool("Bash"))
	h.fake.onGenerate("mid-b", callsTool("Bash"))
	_, err := h.generate(t, "t1", call(commonv1.Role_ROLE_ACT), withSearchTool(t, "找一下"))
	code, pe := errorDetail(t, err)
	if code != connect.CodeInvalidArgument || pe.GetCategory() != providererr.Invalid || pe.GetCode() != "unexpected_tool_call" {
		t.Fatalf("同档都给出没给的工具调用：Invalid unexpected_tool_call（不是 transient，Worker 不会再连打几次）：%v %v/%s", code, pe.GetCategory(), pe.GetCode())
	}
	if n, ok := billedMicros(pe); !ok || n != midACost+midBCost {
		t.Errorf("details.%s 是两次已计费尝试之和：%v（%v）", billedCostDetail, n, ok)
	}
	if h.fake.callsTo("mid-a") != 1 || h.fake.callsTo("mid-b") != 1 {
		t.Errorf("每个 Profile 只试一次：mid-a %d、mid-b %d", h.fake.callsTo("mid-a"), h.fake.callsTo("mid-b"))
	}
	if lines := h.meterLines(t); len(lines) != 2 || lines[0]["status"] != "failed" || lines[1]["status"] != "failed" {
		t.Errorf("两行已计费的失败：%v", lines)
	}
	if strings.Contains(pe.GetMessage(), "command") {
		t.Errorf("错误说明不含工具调用的参数：%q", pe.GetMessage())
	}
}

func TestABilledFailureTellsTheCallerWhatItCost(t *testing.T) {
	h := newHarness(t)
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		usage := &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200}
		return modelprovider.GenerateResult{Text: "不是 JSON", Usage: usage}, providererr.New(providererr.Invalid, "bad_structured_output", "结构化输出不是合法的 JSON")
	})
	g := say("1+1?")
	g.OutputSchema = &commonv1.JsonPayload{Value: jsonValue(t, answerSchema)}
	_, err := h.generate(t, "t1", call(transform), g)
	_, pe := errorDetail(t, err)
	if pe.GetCode() != "bad_structured_output" {
		t.Fatalf("错误照样返回：%s", pe.GetCode())
	}
	if n, ok := billedMicros(pe); !ok || n != midACost {
		t.Errorf("已计费的失败在 details.%s 里带上花费：%v（%v）", billedCostDetail, n, ok)
	}
}

func TestAnUnbilledFailureCarriesNoBilledCost(t *testing.T) {
	h := newHarness(t)
	h.fake.failNext("mid-a", transient("timeout", 0), transient("timeout", 0), transient("timeout", 0))
	h.fake.failNext("mid-b", transient("timeout", 0), transient("timeout", 0), transient("timeout", 0))
	_, err := h.generate(t, "t1", call(transform), say("hi"))
	_, pe := errorDetail(t, err)
	if n, ok := billedMicros(pe); ok {
		t.Errorf("没有计费就不带 %s：%v", billedCostDetail, n)
	}
}

func TestAnAnswerWithoutUsageIsReturnedButFlagged(t *testing.T) {
	h, tel, _ := withTelemetry(t)
	h.fake.onGenerate("mid-a", func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
		return modelprovider.GenerateResult{Text: "OK", Usage: &commonv1.Usage{}}, nil
	})
	resp := h.mustGenerate(t, "t1", call(transform), say("hi"))
	if resp.GetText() != "OK" || resp.GetModel().GetModelProfileId() != "test/mid-a@1" {
		t.Fatalf("零用量的回答照常交给调用方：%v", resp)
	}
	lines := h.meterLines(t)
	if len(lines) != 1 || lines[0]["status"] != "ok" || lines[0]["usage_missing"] != true {
		t.Errorf("计量记 usage_missing: true：%v", lines)
	}
	warned := false
	for _, line := range strings.Split(h.logs.String(), "\n") {
		if strings.Contains(line, "level=WARN") && strings.Contains(line, "test/mid-a@1") && strings.Contains(line, "usage_missing") {
			warned = true
		}
	}
	if !warned {
		t.Errorf("写一条带 Profile 的警告日志：%s", h.logs.String())
	}
	missing := metricsNamed(tel, "runtime.model.usage_missing")
	if len(missing) != 1 || missing[0].Value != 1 || missing[0].Attributes["gen_ai.response.model"] != "mid-a" || missing[0].Attributes["tenant_id"] != "t1" {
		t.Errorf("记一次 runtime.model.usage_missing（属性同 runtime.model.calls）：%+v", missing)
	}
}

func TestAnAnswerWithUsageIsNotFlagged(t *testing.T) {
	h, tel, _ := withTelemetry(t)
	h.mustGenerate(t, "t1", call(transform), say("hi"))
	lines := h.meterLines(t)
	if len(lines) != 1 {
		t.Fatalf("一行计量：%v", lines)
	}
	if v, ok := lines[0]["usage_missing"]; ok && v != false {
		t.Errorf("有用量的回答不记 usage_missing：%v", lines[0])
	}
	if missing := metricsNamed(tel, "runtime.model.usage_missing"); len(missing) != 0 {
		t.Errorf("有用量就不计数：%+v", missing)
	}
}
