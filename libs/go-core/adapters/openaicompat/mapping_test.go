package openaicompat_test

// 验收“请求与响应映射各有测试”：GenerateRequest → chat/completions 的请求体，chat/completions 的响应 → GenerateResult；
// Score 转成结构化输出的生成；Embed 按 index 对回输入顺序。

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat/fakeopenai"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
)

var answerSchema = json.RawMessage(`{"type":"object","required":["answer"],"properties":{"answer":{"type":"string"}},"additionalProperties":false}`)

var lookupParams = json.RawMessage(`{"type":"object","required":["city"],"properties":{"city":{"type":"string"}},"additionalProperties":false}`)

// maxTokensOf 取请求体里的输出上限（max_completion_tokens 或 max_tokens）；都没有时为 nil。
func maxTokensOf(body map[string]any) any {
	if v, ok := body["max_completion_tokens"]; ok {
		return v
	}
	return body["max_tokens"]
}

// textOf 取消息的文本：content 为字符串，或内容片段数组里 type 为 text 的部分拼接。
func textOf(msg any) string {
	switch c := at(msg, "content").(type) {
	case string:
		return c
	case []any:
		var b strings.Builder
		for _, part := range c {
			if at(part, "type") == "text" {
				s, _ := at(part, "text").(string)
				b.WriteString(s)
			}
		}
		return b.String()
	}
	return ""
}

// TestGenerateRequestMapping：消息角色、assistant 的 tool_calls、tool 结果消息（tool_call_id）、tools、模型名、输出上限；
// 非流式请求不带 stream、stream_options；没有 OutputSchema 不带 response_format；思考预算为 0 不带 reasoning_effort。
func TestGenerateRequestMapping(t *testing.T) {
	e := newEnv(t, envConfig{})
	req := modelprovider.GenerateRequest{
		Model: fakeopenai.ChatModel,
		Messages: []modelprovider.Message{
			{Role: modelprovider.RoleSystem, Text: "你是天气助手"},
			{Role: modelprovider.RoleUser, Text: "上海明天天气？"},
			{Role: modelprovider.RoleAssistant, ToolCalls: []modelprovider.ToolCall{{ID: "call_1", Name: "lookup", Arguments: json.RawMessage(`{"city":"上海"}`)}}},
			{Role: modelprovider.RoleTool, ToolCallID: "call_1", Text: `{"forecast":"晴"}`},
			{Role: modelprovider.RoleUser, Text: "那后天呢？", CacheBreakpoint: true},
		},
		Tools:           []modelprovider.ToolSpec{{Name: "lookup", Description: "查天气", ParamsSchema: lookupParams}},
		MaxOutputTokens: 64,
	}
	generate(t, e.tenantA(t), req)
	body := e.lastBody(t, chatPath)

	if body["model"] != fakeopenai.ChatModel {
		t.Errorf("model = %v", body["model"])
	}
	msgs, _ := body["messages"].([]any)
	if len(msgs) != 5 {
		t.Fatalf("messages = %v", body["messages"])
	}
	for i, role := range []string{"system", "user", "assistant", "tool", "user"} {
		if at(msgs[i], "role") != role {
			t.Errorf("messages[%d].role = %v, want %s", i, at(msgs[i], "role"), role)
		}
	}
	if textOf(msgs[0]) != "你是天气助手" || textOf(msgs[1]) != "上海明天天气？" || textOf(msgs[4]) != "那后天呢？" {
		t.Errorf("消息文本不对：%v", msgs)
	}
	call := at(msgs[2], "tool_calls", 0)
	if at(call, "id") != "call_1" || at(call, "type") != "function" || at(call, "function", "name") != "lookup" {
		t.Errorf("assistant.tool_calls[0] = %v", call)
	}
	args, ok := at(call, "function", "arguments").(string)
	if !ok || !jsonEqual(t, args, `{"city":"上海"}`) {
		t.Errorf("tool_calls[0].function.arguments 应为参数 JSON 的字符串，got %#v", at(call, "function", "arguments"))
	}
	if at(msgs[3], "tool_call_id") != "call_1" || !jsonEqual(t, textOf(msgs[3]), `{"forecast":"晴"}`) {
		t.Errorf("tool 消息 = %v", msgs[3])
	}
	tool := at(body, "tools", 0)
	if at(tool, "type") != "function" || at(tool, "function", "name") != "lookup" || at(tool, "function", "description") != "查天气" ||
		!jsonEqual(t, at(tool, "function", "parameters"), lookupParams) {
		t.Errorf("tools[0] = %v", tool)
	}
	if maxTokensOf(body) != float64(64) {
		t.Errorf("输出上限应为 64，body = %v", body)
	}
	for _, absent := range []string{"response_format", "reasoning_effort", "stream_options"} {
		if _, ok := body[absent]; ok {
			t.Errorf("不应发送 %s：%v", absent, body[absent])
		}
	}
	if body["stream"] == true {
		t.Error("非流式请求不应带 stream: true")
	}
}

// TestZeroMaxOutputTokensIsNotSent：MaxOutputTokens 为 0 时不发送输出上限（交给上游默认）。
func TestZeroMaxOutputTokensIsNotSent(t *testing.T) {
	e := newEnv(t, envConfig{})
	req := askFake("x")
	req.MaxOutputTokens = 0
	generate(t, e.tenantA(t), req)
	if v := maxTokensOf(e.lastBody(t, chatPath)); v != nil {
		t.Fatalf("不应发送输出上限：%v", v)
	}
}

// TestStructuredOutputUsesStrictJSONSchema：难点“结构化输出靠上游的 json_schema 严格模式”：
// response_format 为 json_schema、strict 为 true、schema 原样传入；结果放在 Structured，是合法 JSON。
func TestStructuredOutputUsesStrictJSONSchema(t *testing.T) {
	e := newEnv(t, envConfig{})
	req := askFake("1+1 等于几？")
	req.OutputSchema = answerSchema
	r := generate(t, e.tenantA(t), req)
	rf := e.lastBody(t, chatPath)["response_format"]
	if at(rf, "type") != "json_schema" || at(rf, "json_schema", "strict") != true {
		t.Fatalf("response_format = %v", rf)
	}
	if name, _ := at(rf, "json_schema", "name").(string); name == "" {
		t.Fatalf("json_schema.name 不能为空：%v", rf)
	}
	if !jsonEqual(t, at(rf, "json_schema", "schema"), answerSchema) {
		t.Fatalf("schema 应原样传入：%v", at(rf, "json_schema", "schema"))
	}
	want, _ := fakeopenai.Sample(answerSchema)
	if !json.Valid(r.Structured) || !jsonEqual(t, r.Structured, want) {
		t.Fatalf("Structured = %s, want %s", r.Structured, want)
	}
}

// TestStructuredOutputThatIsNotJSONIsInvalid：上游返回非 JSON 的结构化输出时报 Invalid。
func TestStructuredOutputThatIsNotJSONIsInvalid(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{Content: "答案是 2"})
	req := askFake("1+1 等于几？")
	req.OutputSchema = answerSchema
	_, err := e.tenantA(t).Generate(t.Context(), req)
	conformance.ExpectError(t, err, providererr.Invalid, "")
}

// TestReasoningEffortFollowsThinkingBudget：交付 1“思考预算按区间映射到 reasoning_effort（0 不发送）”，看请求体。
func TestReasoningEffortFollowsThinkingBudget(t *testing.T) {
	for _, tc := range []struct {
		budget int
		want   string // 空串表示不发送
	}{{0, ""}, {2048, "low"}, {4096, "low"}, {8000, "medium"}, {16384, "medium"}, {50000, "high"}} {
		e := newEnv(t, envConfig{})
		req := askFake("想一想")
		req.ThinkingBudgetTokens = tc.budget
		generate(t, e.tenantA(t), req)
		got, present := e.lastBody(t, chatPath)["reasoning_effort"]
		if tc.want == "" && present {
			t.Errorf("预算 %d：不应发送 reasoning_effort，got %v", tc.budget, got)
		}
		if tc.want != "" && got != tc.want {
			t.Errorf("预算 %d：reasoning_effort = %v, want %s", tc.budget, got, tc.want)
		}
	}
}

// TestToolCallsAreMapped：响应的 tool_calls → ToolCalls（ID、名字、参数 JSON），finish_reason tool_calls。
func TestToolCallsAreMapped(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{ToolCalls: []fakeopenai.ToolCall{
		{ID: "call_a", Name: "lookup", Arguments: `{"city":"上海"}`},
		{ID: "call_b", Name: "lookup", Arguments: `{"city":"北京"}`},
	}})
	req := askFake("上海和北京的天气")
	req.Tools = []modelprovider.ToolSpec{{Name: "lookup", Description: "查天气", ParamsSchema: lookupParams}}
	r := generate(t, e.tenantA(t), req)
	if r.FinishReason != modelprovider.FinishToolCalls || len(r.ToolCalls) != 2 {
		t.Fatalf("result = %+v", r)
	}
	for i, want := range []struct{ id, city string }{{"call_a", "上海"}, {"call_b", "北京"}} {
		got := r.ToolCalls[i]
		if got.ID != want.id || got.Name != "lookup" || !jsonEqual(t, got.Arguments, `{"city":"`+want.city+`"}`) {
			t.Errorf("ToolCalls[%d] = %+v", i, got)
		}
	}
	if r.Text != "" {
		t.Errorf("只有工具调用时 Text 应为空，got %q", r.Text)
	}
}

// TestFinishReasonMapping：stop → stop，length → max_tokens，tool_calls → tool_calls，content_filter → content_filter（不是错误）。
func TestFinishReasonMapping(t *testing.T) {
	for upstream, want := range map[string]modelprovider.FinishReason{
		"stop":           modelprovider.FinishStop,
		"length":         modelprovider.FinishMaxTokens,
		"tool_calls":     modelprovider.FinishToolCalls,
		"content_filter": modelprovider.FinishContentFilter,
	} {
		e := newEnv(t, envConfig{})
		reply := fakeopenai.Reply{Content: "部分内容", FinishReason: upstream}
		if upstream == "tool_calls" {
			reply.ToolCalls = []fakeopenai.ToolCall{{ID: "call_1", Name: "lookup", Arguments: `{"city":"上海"}`}}
		}
		e.fake.ReplyNext(reply)
		r, err := e.tenantA(t).Generate(t.Context(), askFake("x"))
		if err != nil {
			t.Errorf("finish_reason %s：不应报错：%v", upstream, err)
			continue
		}
		if r.FinishReason != want {
			t.Errorf("finish_reason %s → %q, want %q", upstream, r.FinishReason, want)
		}
	}
}

// TestUsageIsMapped：usage 的各字段（含缓存命中与思考 token）映射到 Usage；成本留给 Model Gateway 按 Model Profile 计价。
func TestUsageIsMapped(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{Content: "好", Usage: &fakeopenai.Usage{PromptTokens: 1200, CompletionTokens: 300, CachedTokens: 1000, ReasoningTokens: 120}})
	r := generate(t, e.tenantA(t), askFake("x"))
	u := r.Usage
	if u == nil || u.GetInputTokens() != 1200 || u.GetOutputTokens() != 300 || u.GetCacheReadTokens() != 1000 ||
		u.GetReasoningTokens() != 120 || u.GetCacheWriteTokens() != 0 || u.GetCostUsdMicros() != 0 {
		t.Fatalf("usage = %+v", u)
	}
}

// TestReasoningContentGoesToThinkingOnly：难点“rationale 只进 trace”：reasoning_content → Thinking，不混进 Text 与 Structured。
func TestReasoningContentGoesToThinkingOnly(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{Content: "答案是 42", ReasoningContent: "先算 6×7"})
	r := generate(t, e.tenantA(t), askFake("6×7？"))
	if r.Text != "答案是 42" || r.Thinking != "先算 6×7" {
		t.Fatalf("Text = %q, Thinking = %q", r.Text, r.Thinking)
	}

	e.fake.ReplyNext(fakeopenai.Reply{Content: `{"answer":"42"}`, ReasoningContent: "先算 6×7"})
	req := askFake("6×7？")
	req.OutputSchema = answerSchema
	r = generate(t, e.tenantA(t), req)
	if !jsonEqual(t, r.Structured, `{"answer":"42"}`) || strings.Contains(string(r.Structured), "先算") || strings.Contains(r.Text, "先算") {
		t.Fatalf("思考内容混进了结果：Text = %q, Structured = %s", r.Text, r.Structured)
	}
	if r.Thinking != "先算 6×7" {
		t.Fatalf("Thinking = %q", r.Thinking)
	}
}

// TestScoreMapsOneScorePerCandidateInOrder：交付 1“Score 转成一次结构化输出的生成（每个候选一个分数）”：
// 请求是带严格 json_schema 的 chat/completions，提示里有 Query、Rubric 与按顺序列出的候选；结果与候选一一对应、顺序不变、带 Usage。
func TestScoreMapsOneScorePerCandidateInOrder(t *testing.T) {
	e := newEnv(t, envConfig{})
	e.fake.ReplyNext(fakeopenai.Reply{Content: `{"scores":[0.9,0.1,0.5]}`, Usage: &fakeopenai.Usage{PromptTokens: 80, CompletionTokens: 12}})
	candidates := []string{"物流破损", "天气晴朗", "尺码不符"}
	r := score(t, e.tenantA(t), modelprovider.ScoreRequest{Model: fakeopenai.ChatModel, Query: "退款原因", Candidates: candidates, Rubric: "与退款原因的相关度"})
	if !slices.Equal(r.Scores, []float64{0.9, 0.1, 0.5}) {
		t.Fatalf("Scores = %v", r.Scores)
	}
	if r.Usage == nil || r.Usage.GetInputTokens() != 80 || r.Usage.GetOutputTokens() != 12 {
		t.Fatalf("Usage = %+v", r.Usage)
	}
	if e.posts(chatPath) != 1 {
		t.Fatalf("Score 应是一次 chat/completions 调用，requests = %d", e.posts(chatPath))
	}
	body := e.lastBody(t, chatPath)
	if body["model"] != fakeopenai.ChatModel || at(body, "response_format", "type") != "json_schema" || at(body, "response_format", "json_schema", "strict") != true {
		t.Fatalf("Score 的请求应是严格 json_schema 的结构化输出：%v", body)
	}
	var prompt strings.Builder
	for _, m := range body["messages"].([]any) {
		prompt.WriteString(textOf(m))
		prompt.WriteString("\n")
	}
	text := prompt.String()
	for _, want := range []string{"退款原因", "与退款原因的相关度"} {
		if !strings.Contains(text, want) {
			t.Errorf("提示里缺少 %q", want)
		}
	}
	last := -1
	for _, c := range candidates {
		i := strings.Index(text, c)
		if i <= last {
			t.Fatalf("候选应按顺序出现在提示里：%q", text)
		}
		last = i
	}
}

// TestScoreRejectsWrongNumberOfScores：上游给的分数个数不对、或不是合法 JSON 时报错，不返回部分结果。
func TestScoreRejectsWrongNumberOfScores(t *testing.T) {
	for _, content := range []string{`{"scores":[0.5]}`, `{"scores":[0.1,0.2,0.3,0.4]}`, `{"scores":[]}`, `不是 JSON`} {
		e := newEnv(t, envConfig{})
		e.fake.ReplyNext(fakeopenai.Reply{Content: content})
		r, err := e.tenantA(t).Score(t.Context(), modelprovider.ScoreRequest{Model: fakeopenai.ChatModel, Query: "q", Candidates: []string{"a", "b", "c"}})
		if err == nil {
			t.Errorf("上游回复 %s 时应报错，got %v", content, r.Scores)
			continue
		}
		expectProviderError(t, err)
		if r.Scores != nil {
			t.Errorf("报错时不应返回部分结果：%v", r.Scores)
		}
	}
}

// TestEmbedKeepsInputOrderByIndex：Embed 的 input 为文本数组；上游倒序返回时按 index 对回输入顺序；带 Usage。
func TestEmbedKeepsInputOrderByIndex(t *testing.T) {
	e := newEnv(t, envConfig{fake: fakeopenai.Options{ReverseEmbeddings: true}})
	texts := []string{"alpha", "beta", "gamma"}
	r := embed(t, e.tenantA(t), modelprovider.EmbedRequest{Model: fakeopenai.EmbeddingModel, Texts: texts})
	if len(r.Vectors) != 3 {
		t.Fatalf("vectors = %d", len(r.Vectors))
	}
	for i, text := range texts {
		if !slices.Equal(r.Vectors[i], fakeopenai.Vector(text, fakeopenai.DefaultEmbeddingDims)) {
			t.Errorf("Vectors[%d] 不是 %q 的向量", i, text)
		}
	}
	if r.Usage == nil || r.Usage.GetInputTokens() <= 0 {
		t.Fatalf("Usage = %+v", r.Usage)
	}
	body := e.lastBody(t, embedPath)
	if body["model"] != fakeopenai.EmbeddingModel || !jsonEqual(t, body["input"], texts) {
		t.Fatalf("embeddings 请求 = %v", body)
	}
}

// TestUnknownModelIsNotFound：难点“模型不存在为 not_found”：上游 404 model_not_found（生成与嵌入）。
func TestUnknownModelIsNotFound(t *testing.T) {
	e := newEnv(t, envConfig{})
	c := e.tenantA(t)
	_, err := c.Generate(t.Context(), ask("no-such-model", "x"))
	conformance.ExpectError(t, err, providererr.NotFound, "")
	_, err = c.Embed(t.Context(), modelprovider.EmbedRequest{Model: "no-such-embedding", Texts: []string{"a"}})
	conformance.ExpectError(t, err, providererr.NotFound, "")
	_, err = c.Score(t.Context(), modelprovider.ScoreRequest{Model: "no-such-model", Query: "q", Candidates: []string{"a"}})
	conformance.ExpectError(t, err, providererr.NotFound, "")
}
