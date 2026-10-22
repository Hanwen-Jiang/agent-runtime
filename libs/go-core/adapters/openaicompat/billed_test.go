package openaicompat_test

// R-02 评审（docs/plan/review-R-02-model-gateway.md）处理建议第 3 条的适配层一侧：上游回了 200 并已计费，但结果不合格
// （结构化输出不是合法 JSON、工具参数不是 JSON、Score 的个数不对）时，照样报错，同时在结果里带回上游的 Usage，
// Model Gateway 才能把这次调用记进计量。上游没有回结果的失败（HTTP 错误、超时、断开）没有 Usage。

import (
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat/fakeopenai"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

var billed = &fakeopenai.Usage{PromptTokens: 900, CompletionTokens: 120, CachedTokens: 300, ReasoningTokens: 40}

func assertBilledUsage(t *testing.T, what string, got *commonv1.Usage) {
	t.Helper()
	if got == nil || got.GetInputTokens() != 900 || got.GetOutputTokens() != 120 || got.GetCacheReadTokens() != 300 {
		t.Errorf("%s：报错时也要带回上游的 Usage，got %+v", what, got)
	}
}

func TestInvalidStructuredOutputStillReturnsUsage(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{Content: "答案是 2", Usage: billed})
	req := askFake("1+1 等于几？")
	req.OutputSchema = answerSchema
	r, err := e.tenantA(t).Generate(t.Context(), req)
	conformance.ExpectError(t, err, providererr.Invalid, "bad_structured_output")
	assertBilledUsage(t, "结构化输出不是合法 JSON", r.Usage)
	if r.Structured != nil {
		t.Errorf("不合格的结构化输出不能当结果：%s", r.Structured)
	}
}

func TestInvalidToolArgumentsStillReturnUsage(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{ToolCalls: []fakeopenai.ToolCall{{ID: "call_1", Name: "lookup", Arguments: "{城市: 上海"}}, Usage: billed})
	req := askFake("上海天气")
	req.Tools = []modelprovider.ToolSpec{{Name: "lookup", Description: "查天气", ParamsSchema: lookupParams}}
	r, err := e.tenantA(t).Generate(t.Context(), req)
	conformance.ExpectError(t, err, providererr.Invalid, "bad_tool_arguments")
	assertBilledUsage(t, "工具参数不是 JSON", r.Usage)
}

func TestWrongNumberOfScoresStillReturnsUsage(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{Content: `{"scores":[0.5]}`, Usage: billed})
	r, err := e.tenantA(t).Score(t.Context(), modelprovider.ScoreRequest{Model: fakeopenai.ChatModel, Query: "q", Candidates: []string{"a", "b"}})
	conformance.ExpectError(t, err, providererr.Invalid, "bad_scores")
	if r.Scores != nil {
		t.Errorf("个数不对时不返回分数：%v", r.Scores)
	}
	assertBilledUsage(t, "Score 的个数不对", r.Usage)
}

func TestUpstreamErrorsCarryNoUsage(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.FailNext(fakeopenai.Fault{Path: chatPath, Status: 500})
	r, err := e.tenantA(t).Generate(t.Context(), askFake("x"))
	conformance.ExpectError(t, err, providererr.Transient, "")
	if r.Usage != nil {
		t.Errorf("上游没有回结果，没有 Usage：%+v", r.Usage)
	}
}
