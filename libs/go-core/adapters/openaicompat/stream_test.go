package openaicompat_test

// 难点“流式”：请求 stream_options.include_usage；各分片文本依次拼接等于最后一个分片的文本；最后一个分片带 Usage。

import (
	"encoding/json"
	"errors"
	"io"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat/fakeopenai"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
)

// TestStreamRequestsUsageAndDeltasConcatenateToFinal：请求带 stream 与 stream_options.include_usage；TextDelta 拼接等于 Final.Text；
// Final 带上游末尾分片的 Usage 与 finish_reason；Final 之后 Recv 返回 io.EOF（再读仍是 io.EOF）。
func TestStreamRequestsUsageAndDeltasConcatenateToFinal(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{Content: "一二三四五，上山打老虎。", Usage: &fakeopenai.Usage{PromptTokens: 40, CompletionTokens: 9, CachedTokens: 32}})
	s, err := e.tenantA(t).StreamGenerate(t.Context(), askFake("数到五"))
	if err != nil {
		t.Fatalf("StreamGenerate: %v", err)
	}
	defer s.Close()
	d := drain(t.Context(), s)
	if d.err != nil {
		t.Fatalf("Recv: %v", d.err)
	}
	body := e.lastBody(t, chatPath)
	if body["stream"] != true || at(body, "stream_options", "include_usage") != true {
		t.Fatalf("流式请求应带 stream 与 stream_options.include_usage：%v", body)
	}
	if d.final == nil || d.final.Text != "一二三四五，上山打老虎。" || d.text.String() != d.final.Text {
		t.Fatalf("deltas = %q, final = %+v", d.text.String(), d.final)
	}
	if len(d.chunks) < 3 {
		t.Fatalf("上游分了多片，适配层应逐片交出，got %d 个分片", len(d.chunks))
	}
	if last := d.chunks[len(d.chunks)-1]; last.Final == nil {
		t.Fatal("最后一个分片应是 Final")
	}
	u := d.final.Usage
	if u == nil || u.GetInputTokens() != 40 || u.GetOutputTokens() != 9 || u.GetCacheReadTokens() != 32 {
		t.Fatalf("Final.Usage = %+v", u)
	}
	if d.final.FinishReason != modelprovider.FinishStop {
		t.Fatalf("FinishReason = %q", d.final.FinishReason)
	}
	for range 2 {
		if _, err := s.Recv(t.Context()); !errors.Is(err, io.EOF) {
			t.Fatalf("Final 之后 Recv 应返回 io.EOF，got %v", err)
		}
	}
}

// TestStreamSkipsSSEComments：上游在数据之间夹 SSE 注释行（": keep-alive"）时照常解析。
func TestStreamSkipsSSEComments(t *testing.T) {
	e := newEnv(t, envConfig{fake: fakeopenai.Options{SSEComments: true}})
	e.fake.ReplyNext(fakeopenai.Reply{Content: "注释不影响内容"})
	d := stream(t, e.tenantA(t), askFake("x"))
	if d.final == nil || d.final.Text != "注释不影响内容" || d.text.String() != d.final.Text || d.final.Usage == nil {
		t.Fatalf("deltas = %q, final = %+v", d.text.String(), d.final)
	}
}

// TestStreamThinkingDeltasStaySeparate：reasoning_content 的分片 → ThinkingDelta，拼接等于 Final.Thinking，不混进 TextDelta 与 Text。
func TestStreamThinkingDeltasStaySeparate(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{Content: "答案是 42", ReasoningContent: "先算六乘七，得四十二"})
	d := stream(t, e.tenantA(t), askFake("6×7？"))
	if d.final == nil {
		t.Fatal("没有 Final")
	}
	if d.thinking.String() != "先算六乘七，得四十二" || d.final.Thinking != d.thinking.String() {
		t.Fatalf("thinking deltas = %q, Final.Thinking = %q", d.thinking.String(), d.final.Thinking)
	}
	if d.text.String() != "答案是 42" || d.final.Text != "答案是 42" {
		t.Fatalf("text deltas = %q, Final.Text = %q", d.text.String(), d.final.Text)
	}
}

// TestStreamToolCallsAreAssembled：工具调用的参数分片拼完整后才交出：每个带 ToolCall 的分片都是完整的调用（参数是合法 JSON），
// 每个调用恰好交出一次；Final.ToolCalls 与之相同，FinishReason 为 tool_calls。
func TestStreamToolCallsAreAssembled(t *testing.T) {
	e := newEnv(t, envConfig{})
	calls := []fakeopenai.ToolCall{
		{ID: "call_a", Name: "lookup", Arguments: `{"city":"上海","days":3}`},
		{ID: "call_b", Name: "lookup", Arguments: `{"city":"北京","days":1}`},
	}
	e.fake.ReplyNext(fakeopenai.Reply{ToolCalls: calls})
	req := askFake("上海与北京的天气")
	req.Tools = []modelprovider.ToolSpec{{Name: "lookup", Description: "查天气", ParamsSchema: lookupParams}}
	d := stream(t, e.tenantA(t), req)
	if d.final == nil || d.final.FinishReason != modelprovider.FinishToolCalls || len(d.final.ToolCalls) != 2 {
		t.Fatalf("final = %+v", d.final)
	}
	var emitted []modelprovider.ToolCall
	for _, c := range d.chunks {
		if c.ToolCall != nil {
			if !json.Valid(c.ToolCall.Arguments) || c.ToolCall.Name == "" || c.ToolCall.ID == "" {
				t.Fatalf("交出的工具调用不完整：%+v", c.ToolCall)
			}
			emitted = append(emitted, *c.ToolCall)
		}
	}
	if len(emitted) != 2 {
		t.Fatalf("每个工具调用应恰好交出一次，got %d", len(emitted))
	}
	for i, want := range calls {
		for _, got := range []modelprovider.ToolCall{emitted[i], d.final.ToolCalls[i]} {
			if got.ID != want.ID || got.Name != want.Name || !jsonEqual(t, got.Arguments, want.Arguments) {
				t.Fatalf("ToolCall[%d] = %+v, want %+v", i, got, want)
			}
		}
	}
}

// TestStreamStructuredOutput：流式的结构化输出：Final.Structured 是拼接后的合法 JSON。
func TestStreamStructuredOutput(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{Content: `{"answer":"二"}`})
	req := askFake("1+1？")
	req.OutputSchema = answerSchema
	d := stream(t, e.tenantA(t), req)
	if d.final == nil || !jsonEqual(t, d.final.Structured, `{"answer":"二"}`) {
		t.Fatalf("final = %+v", d.final)
	}
	if at(e.lastBody(t, chatPath), "response_format", "json_schema", "strict") != true {
		t.Fatal("流式的结构化输出也要用严格 json_schema")
	}
}

// TestStreamTruncatedIsTransient：上游没有发出 [DONE] 就断开时，Recv 返回 Transient，不交出 Final。
func TestStreamTruncatedIsTransient(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{Content: "这段回复会在中途断开，后面的内容收不到"})
	e.fake.FailNext(fakeopenai.Fault{Path: chatPath, CutStreamAfter: 3})
	d, err := streamErr(t.Context(), e.tenantA(t), askFake("x"))
	conformance.ExpectError(t, err, providererr.Transient, "")
	if d != nil && d.final != nil {
		t.Fatalf("断开的流不应交出 Final：%+v", d.final)
	}
}

// TestStreamUpstreamErrorsAreClassified：流式开头的 HTTP 错误按同一规则分类（StreamGenerate 或第一次 Recv 返回）；
// 429 带 RetryAfter。
func TestStreamUpstreamErrorsAreClassified(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.FailNext(fakeopenai.RateLimit(chatPath, "2"))
	_, err := streamErr(t.Context(), e.tenantA(t), askFake("x"))
	conformance.ExpectError(t, err, providererr.Transient, "")
	if pe := expectProviderError(t, err); pe.RetryAfter != 2*time.Second {
		t.Fatalf("RetryAfter = %v, want 2s", pe.RetryAfter)
	}
	e.fake.FailNext(fakeopenai.Fault{Path: chatPath, Status: 401, Code: "invalid_api_key"})
	_, err = streamErr(t.Context(), e.tenantA(t), askFake("x"))
	conformance.ExpectError(t, err, providererr.Denied, "")
	_, err = streamErr(t.Context(), e.tenantA(t), ask("no-such-model", "x"))
	conformance.ExpectError(t, err, providererr.NotFound, "")
}

// TestStreamTimeoutIsTransient：流式调用超过 Options.Timeout 为 Transient。
func TestStreamTimeoutIsTransient(t *testing.T) {
	e := newEnv(t, envConfig{adapter: func(o *openaicompat.Options) { o.Timeout = 300 * time.Millisecond }})
	e.fake.FailNext(fakeopenai.Fault{Path: chatPath, Delay: 5 * time.Second})
	start := time.Now()
	_, err := streamErr(t.Context(), e.tenantA(t), askFake("x"))
	conformance.ExpectError(t, err, providererr.Transient, "")
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("超时没有生效：%v", elapsed)
	}
}
