package openaicompat

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// schemaInstruction 放在结构化输出请求的最后一条 user 消息里（不执行 response_format 的端点靠它）。
const schemaInstruction = "只输出一个符合下面 JSON Schema 的 JSON 对象，不要输出其他文字，也不要用代码块包起来：\n"

// strictUnsupported 是 json_schema 严格模式不接受的关键字：schema 里出现任何一个就不发 strict。
var strictUnsupported = []string{
	"oneOf", "allOf", "not", "if", "then", "else", "patternProperties", "dependentSchemas", "dependentRequired",
	"unevaluatedProperties", "unevaluatedItems", "minLength", "maxLength", "minProperties", "maxProperties", "uniqueItems", "contains",
}

// strictCompatible 判断 schema 能不能用 json_schema 的严格模式：每个 object 都写 additionalProperties: false，required 列出全部属性，
// 不用严格模式不接受的关键字（T-13 真模型端到端发现：不满足时 OpenAI 拒绝整个请求）。不满足时发 strict: false，模型照样按 schema 输出，
// 结果由调用方校验（Python 侧按 schema 校验，不合格重问）。
func strictCompatible(raw json.RawMessage) bool {
	var schema any
	if json.Unmarshal(raw, &schema) != nil {
		return false
	}
	return strictNode(schema)
}

func strictNode(node any) bool {
	switch v := node.(type) {
	case []any:
		for _, item := range v {
			if !strictNode(item) {
				return false
			}
		}
		return true
	case map[string]any:
		for _, k := range strictUnsupported {
			if _, ok := v[k]; ok {
				return false
			}
		}
		props, hasProps := v["properties"].(map[string]any)
		if v["type"] == "object" || hasProps {
			if v["additionalProperties"] != false {
				return false
			}
			required, _ := v["required"].([]any)
			if len(required) != len(props) {
				return false
			}
			for _, r := range required {
				name, _ := r.(string)
				if _, ok := props[name]; !ok {
					return false
				}
			}
		}
		for _, k := range []string{"properties", "$defs", "definitions"} {
			if children, ok := v[k].(map[string]any); ok {
				for _, child := range children {
					if !strictNode(child) {
						return false
					}
				}
			}
		}
		for _, k := range []string{"items", "prefixItems", "anyOf"} {
			if child, ok := v[k]; ok && !strictNode(child) {
				return false
			}
		}
		return true
	default:
		return true
	}
}

// ---- 请求 ----

type wirePart struct {
	Type         string         `json:"type"`
	Text         string         `json:"text"`
	CacheControl map[string]any `json:"cache_control,omitempty"`
}

type wireFunction struct {
	Name      string `json:"name,omitempty"`
	Arguments string `json:"arguments"`
}

type wireToolCall struct {
	Index    *int         `json:"index,omitempty"`
	ID       string       `json:"id,omitempty"`
	Type     string       `json:"type,omitempty"`
	Function wireFunction `json:"function"`
}

type wireMessage struct {
	Role string `json:"role"`
	// Content 是字符串、内容片段数组，或 null（只有工具调用的 assistant 消息）。
	Content    any            `json:"content"`
	ToolCalls  []wireToolCall `json:"tool_calls,omitempty"`
	ToolCallID string         `json:"tool_call_id,omitempty"`
}

type wireTool struct {
	Type     string `json:"type"`
	Function struct {
		Name        string          `json:"name"`
		Description string          `json:"description,omitempty"`
		Parameters  json.RawMessage `json:"parameters"`
	} `json:"function"`
}

type wireJSONSchema struct {
	Name   string          `json:"name"`
	Schema json.RawMessage `json:"schema"`
	Strict bool            `json:"strict"`
}

type wireResponseFormat struct {
	Type       string          `json:"type"`
	JSONSchema *wireJSONSchema `json:"json_schema,omitempty"`
}

type wireStreamOptions struct {
	IncludeUsage bool `json:"include_usage"`
}

type wireRequest struct {
	Model               string              `json:"model"`
	Messages            []wireMessage       `json:"messages"`
	Tools               []wireTool          `json:"tools,omitempty"`
	ResponseFormat      *wireResponseFormat `json:"response_format,omitempty"`
	MaxCompletionTokens int                 `json:"max_completion_tokens,omitempty"`
	ReasoningEffort     string              `json:"reasoning_effort,omitempty"`
	Stream              bool                `json:"stream,omitempty"`
	StreamOptions       *wireStreamOptions  `json:"stream_options,omitempty"`
}

var emptyParams = json.RawMessage(`{"type":"object","properties":{}}`)

var roles = map[modelprovider.Role]string{
	modelprovider.RoleSystem:    "system",
	modelprovider.RoleUser:      "user",
	modelprovider.RoleAssistant: "assistant",
	modelprovider.RoleTool:      "tool",
}

func invalid(code, message string) *providererr.Error {
	return providererr.New(providererr.Invalid, code, message)
}

// chatRequest 把 GenerateRequest 转成 chat/completions 的请求体（规则见包注释）。
func chatRequest(req modelprovider.GenerateRequest, stream bool) (*wireRequest, error) {
	if strings.TrimSpace(req.Model) == "" {
		return nil, invalid("missing_model", "缺少模型名")
	}
	if len(req.Messages) == 0 {
		return nil, invalid("empty_messages", "消息为空")
	}
	out := &wireRequest{Model: req.Model, MaxCompletionTokens: max(req.MaxOutputTokens, 0), ReasoningEffort: EffortForBudget(req.ThinkingBudgetTokens)}
	for i, m := range req.Messages {
		role, ok := roles[m.Role]
		if !ok {
			return nil, invalid("bad_message", fmt.Sprintf("第 %d 条消息的角色不对", i))
		}
		w := wireMessage{Role: role, Content: m.Text, ToolCallID: m.ToolCallID}
		if m.CacheBreakpoint && m.Text != "" {
			w.Content = []wirePart{{Type: "text", Text: m.Text, CacheControl: map[string]any{"type": "ephemeral"}}}
		}
		for _, call := range m.ToolCalls {
			args := strings.TrimSpace(string(call.Arguments))
			if args == "" {
				args = "{}"
			}
			w.ToolCalls = append(w.ToolCalls, wireToolCall{ID: call.ID, Type: "function", Function: wireFunction{Name: call.Name, Arguments: args}})
		}
		if role == "assistant" && m.Text == "" && len(m.ToolCalls) > 0 {
			w.Content = nil
		}
		if role == "tool" && m.ToolCallID == "" {
			return nil, invalid("bad_message", fmt.Sprintf("第 %d 条消息是工具结果，缺 tool_call_id", i))
		}
		out.Messages = append(out.Messages, w)
	}
	for _, t := range req.Tools {
		var w wireTool
		w.Type = "function"
		w.Function.Name, w.Function.Description, w.Function.Parameters = t.Name, t.Description, t.ParamsSchema
		if len(w.Function.Parameters) == 0 {
			w.Function.Parameters = emptyParams
		}
		out.Tools = append(out.Tools, w)
	}
	if len(req.OutputSchema) > 0 {
		if !json.Valid(req.OutputSchema) {
			return nil, invalid("bad_schema", "output_schema 不是合法的 JSON")
		}
		out.ResponseFormat = &wireResponseFormat{Type: "json_schema", JSONSchema: &wireJSONSchema{Name: "output", Schema: req.OutputSchema, Strict: strictCompatible(req.OutputSchema)}}
		out.Messages = append(out.Messages, wireMessage{Role: "user", Content: schemaInstruction + string(req.OutputSchema)})
	}
	if stream {
		out.Stream, out.StreamOptions = true, &wireStreamOptions{IncludeUsage: true}
	}
	return out, nil
}

// ---- 响应 ----

type wireUsage struct {
	PromptTokens        int64 `json:"prompt_tokens"`
	CompletionTokens    int64 `json:"completion_tokens"`
	PromptTokensDetails *struct {
		CachedTokens int64 `json:"cached_tokens"`
	} `json:"prompt_tokens_details"`
	CompletionTokensDetails *struct {
		ReasoningTokens int64 `json:"reasoning_tokens"`
	} `json:"completion_tokens_details"`
}

func (u *wireUsage) proto() *commonv1.Usage {
	if u == nil {
		return &commonv1.Usage{}
	}
	out := &commonv1.Usage{InputTokens: u.PromptTokens, OutputTokens: u.CompletionTokens}
	if u.PromptTokensDetails != nil {
		out.CacheReadTokens = u.PromptTokensDetails.CachedTokens
	}
	if u.CompletionTokensDetails != nil {
		out.ReasoningTokens = u.CompletionTokensDetails.ReasoningTokens
	}
	return out
}

type wireReplyMessage struct {
	Content          json.RawMessage `json:"content"`
	ReasoningContent string          `json:"reasoning_content"`
	// Reasoning 是部分网关放思考内容的字段名。
	Reasoning string         `json:"reasoning"`
	ToolCalls []wireToolCall `json:"tool_calls"`
}

type wireChoice struct {
	Message      wireReplyMessage `json:"message"`
	FinishReason string           `json:"finish_reason"`
}

type wireResponse struct {
	Choices []wireChoice `json:"choices"`
	Usage   *wireUsage   `json:"usage"`
}

// contentText 取 content 的文本：字符串、null，或内容片段数组里 type 为 text 的部分拼接。
func contentText(raw json.RawMessage) string {
	if len(raw) == 0 || string(raw) == "null" {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var parts []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &parts) != nil {
		return ""
	}
	var b strings.Builder
	for _, p := range parts {
		if p.Type == "text" {
			b.WriteString(p.Text)
		}
	}
	return b.String()
}

func finishReason(upstream string, hasToolCalls bool) modelprovider.FinishReason {
	switch upstream {
	case "length":
		return modelprovider.FinishMaxTokens
	case "tool_calls", "function_call":
		return modelprovider.FinishToolCalls
	case "content_filter":
		return modelprovider.FinishContentFilter
	case "stop":
		return modelprovider.FinishStop
	}
	if hasToolCalls {
		return modelprovider.FinishToolCalls
	}
	return modelprovider.FinishStop
}

// arguments 把工具调用的参数字符串转成 JSON；空串为 {}，不是合法 JSON 报 Invalid。
func arguments(raw string) (json.RawMessage, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return json.RawMessage(`{}`), nil
	}
	if !json.Valid([]byte(raw)) {
		return nil, invalid("bad_tool_arguments", "模型给出的工具参数不是合法的 JSON")
	}
	return json.RawMessage(raw), nil
}

// structured 从文本里取出结构化输出：容忍 ```json 代码块；不是合法 JSON 报 Invalid。
func structured(text string) (json.RawMessage, error) {
	body := strings.TrimSpace(text)
	if strings.HasPrefix(body, "```") {
		body = strings.TrimPrefix(body, "```json")
		body = strings.TrimPrefix(body, "```")
		body = strings.TrimSuffix(strings.TrimSpace(body), "```")
		body = strings.TrimSpace(body)
	}
	if body == "" || !json.Valid([]byte(body)) {
		return nil, invalid("bad_structured_output", "结构化输出不是合法的 JSON")
	}
	return json.RawMessage(body), nil
}

// result 把一个 choice 转成 GenerateResult。工具参数或结构化输出不合格时报 Invalid，同时在结果里带回 Usage
// （上游已经计费，Model Gateway 要记进计量）。
func result(choice wireChoice, usage *wireUsage, schema json.RawMessage) (modelprovider.GenerateResult, error) {
	m := choice.Message
	out := modelprovider.GenerateResult{
		Text:         contentText(m.Content),
		Thinking:     m.ReasoningContent,
		FinishReason: finishReason(choice.FinishReason, len(m.ToolCalls) > 0),
		Usage:        usage.proto(),
	}
	if out.Thinking == "" {
		out.Thinking = m.Reasoning
	}
	for _, call := range m.ToolCalls {
		args, err := arguments(call.Function.Arguments)
		if err != nil {
			return modelprovider.GenerateResult{Usage: out.Usage}, err
		}
		out.ToolCalls = append(out.ToolCalls, modelprovider.ToolCall{ID: call.ID, Name: call.Function.Name, Arguments: args})
	}
	if len(schema) > 0 && len(out.ToolCalls) == 0 {
		s, err := structured(out.Text)
		if err != nil {
			return modelprovider.GenerateResult{Usage: out.Usage}, err
		}
		out.Structured = s
	}
	return out, nil
}

// ---- 方法 ----

func (c *client) Generate(ctx context.Context, req modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
	body, err := chatRequest(req, false)
	if err != nil {
		return modelprovider.GenerateResult{}, err
	}
	var resp wireResponse
	if err := c.postJSON(ctx, chatPath, req.Model, body, &resp); err != nil {
		return modelprovider.GenerateResult{}, err
	}
	if len(resp.Choices) == 0 {
		return modelprovider.GenerateResult{}, providererr.New(providererr.Transient, "empty_choices", "上游的回答里没有 choices")
	}
	return result(resp.Choices[0], resp.Usage, req.OutputSchema)
}

// scorePrompt 与 scoreSchema：Score 转成一次结构化输出的生成（规则见包注释）。
func scoreRequest(req modelprovider.ScoreRequest) (modelprovider.GenerateRequest, error) {
	if len(req.Candidates) == 0 {
		return modelprovider.GenerateRequest{}, invalid("empty_candidates", "没有候选")
	}
	var b strings.Builder
	b.WriteString("查询：" + req.Query + "\n")
	if req.Rubric != "" {
		b.WriteString("评分标准：" + req.Rubric + "\n")
	}
	fmt.Fprintf(&b, "下面按顺序列出 %d 个候选。按评分标准给每个候选一个 0 到 1 之间的分数，越符合越高；scores 按候选的顺序排列，个数与候选相同。\n", len(req.Candidates))
	for i, cand := range req.Candidates {
		fmt.Fprintf(&b, "候选 %d：%s\n", i+1, cand)
	}
	n := len(req.Candidates)
	schema := fmt.Sprintf(`{"type":"object","properties":{"scores":{"type":"array","items":{"type":"number"},"minItems":%d,"maxItems":%d}},"required":["scores"],"additionalProperties":false}`, n, n)
	return modelprovider.GenerateRequest{
		Model:           req.Model,
		MaxOutputTokens: req.MaxOutputTokens,
		Messages:        []modelprovider.Message{{Role: modelprovider.RoleSystem, Text: "你是打分器，只按给定的评分标准打分。"}, {Role: modelprovider.RoleUser, Text: b.String()}},
		OutputSchema:    json.RawMessage(schema),
	}, nil
}

func (c *client) Score(ctx context.Context, req modelprovider.ScoreRequest) (modelprovider.ScoreResult, error) {
	gen, err := scoreRequest(req)
	if err != nil {
		return modelprovider.ScoreResult{}, err
	}
	r, err := c.Generate(ctx, gen)
	if err != nil {
		return modelprovider.ScoreResult{Usage: r.Usage}, err
	}
	var parsed struct {
		Scores []float64 `json:"scores"`
	}
	if json.Unmarshal(r.Structured, &parsed) != nil || len(parsed.Scores) != len(req.Candidates) {
		// 上游已经计费：分数不对也带回 Usage。
		return modelprovider.ScoreResult{Usage: r.Usage}, invalid("bad_scores", fmt.Sprintf("分数的个数应为 %d", len(req.Candidates)))
	}
	return modelprovider.ScoreResult{Scores: parsed.Scores, Usage: r.Usage}, nil
}

type wireEmbedding struct {
	Index     int             `json:"index"`
	Embedding json.RawMessage `json:"embedding"`
}

// vector 解析一个向量：浮点数组，或 base64 编码的小端 float32。
func vector(raw json.RawMessage) ([]float32, bool) {
	var floats []float32
	if json.Unmarshal(raw, &floats) == nil {
		return floats, len(floats) > 0
	}
	var encoded string
	if json.Unmarshal(raw, &encoded) != nil {
		return nil, false
	}
	buf, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil || len(buf) == 0 || len(buf)%4 != 0 {
		return nil, false
	}
	out := make([]float32, len(buf)/4)
	for i := range out {
		out[i] = math.Float32frombits(binary.LittleEndian.Uint32(buf[i*4:]))
	}
	return out, true
}

func (c *client) Embed(ctx context.Context, req modelprovider.EmbedRequest) (modelprovider.EmbedResult, error) {
	if strings.TrimSpace(req.Model) == "" {
		return modelprovider.EmbedResult{}, invalid("missing_model", "缺少模型名")
	}
	if len(req.Texts) == 0 {
		return modelprovider.EmbedResult{}, invalid("empty_texts", "没有要嵌入的文本")
	}
	var resp struct {
		Data  []wireEmbedding `json:"data"`
		Usage *wireUsage      `json:"usage"`
	}
	if err := c.postJSON(ctx, embedPath, req.Model, map[string]any{"model": req.Model, "input": req.Texts, "encoding_format": "float"}, &resp); err != nil {
		return modelprovider.EmbedResult{}, err
	}
	vectors := make([][]float32, len(req.Texts))
	for _, d := range resp.Data {
		v, ok := vector(d.Embedding)
		if !ok || d.Index < 0 || d.Index >= len(vectors) || vectors[d.Index] != nil {
			return modelprovider.EmbedResult{}, providererr.New(providererr.Transient, "bad_response", "上游的嵌入结果不完整")
		}
		vectors[d.Index] = v
	}
	for _, v := range vectors {
		if v == nil {
			return modelprovider.EmbedResult{}, providererr.New(providererr.Transient, "bad_response", "上游的嵌入结果不完整")
		}
	}
	usage := &commonv1.Usage{}
	if resp.Usage != nil {
		usage.InputTokens = resp.Usage.PromptTokens
	}
	return modelprovider.EmbedResult{Vectors: vectors, Usage: usage}, nil
}
