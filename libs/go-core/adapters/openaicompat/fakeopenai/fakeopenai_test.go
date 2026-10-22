package fakeopenai_test

// 模拟服务自身的测试：它是其他测试的依据，行为要照 OpenAI 兼容协议，故障注入要可靠。

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/http"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat/fakeopenai"
)

const key = "sk-test-DO-NOT-LEAK-0123"

func post(t *testing.T, s *fakeopenai.Server, path, apiKey string, body any) (*http.Response, []byte) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	req, err := http.NewRequestWithContext(t.Context(), http.MethodPost, s.URL+path, bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+apiKey)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	out, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	return resp, out
}

func decode(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("响应不是 JSON：%s", raw)
	}
	return m
}

func at(v any, path ...any) any {
	for _, p := range path {
		switch k := p.(type) {
		case string:
			m, _ := v.(map[string]any)
			v = m[k]
		case int:
			s, _ := v.([]any)
			if k >= len(s) {
				return nil
			}
			v = s[k]
		}
	}
	return v
}

func chatBody(extra map[string]any) map[string]any {
	b := map[string]any{"model": fakeopenai.ChatModel, "messages": []any{map[string]any{"role": "user", "content": "你好"}}}
	for k, v := range extra {
		b[k] = v
	}
	return b
}

func TestChatCompletionDefaultReply(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{APIKeys: []string{key}})
	resp, raw := post(t, s, "/chat/completions", key, chatBody(nil))
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d: %s", resp.StatusCode, raw)
	}
	m := decode(t, raw)
	if at(m, "choices", 0, "message", "content") != "fake reply: 你好" || at(m, "choices", 0, "finish_reason") != "stop" {
		t.Fatalf("reply = %s", raw)
	}
	if at(m, "usage", "prompt_tokens").(float64) <= 0 || at(m, "usage", "completion_tokens").(float64) <= 0 ||
		at(m, "usage", "prompt_tokens_details", "cached_tokens") == nil || at(m, "usage", "completion_tokens_details", "reasoning_tokens") == nil {
		t.Fatalf("usage = %v", m["usage"])
	}
	reqs := s.Requests()
	if len(reqs) != 1 || reqs[0].Path != "/chat/completions" || reqs[0].APIKey != key || reqs[0].JSON()["model"] != fakeopenai.ChatModel {
		t.Fatalf("requests = %+v", reqs)
	}
}

// TestRequestRecordsRawURI：记录的请求带原始 URI（含查询串），Path 仍去掉 /v1 与查询串；
// 适配层的测试据此检查密钥没有放进 URL。
func TestRequestRecordsRawURI(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{APIKeys: []string{key}})
	resp, raw := post(t, s, "/chat/completions?api-version=2026-10&probe=1", key, chatBody(nil))
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d: %s", resp.StatusCode, raw)
	}
	reqs := s.Requests()
	if len(reqs) != 1 {
		t.Fatalf("requests = %d", len(reqs))
	}
	if reqs[0].Path != "/chat/completions" || reqs[0].RawURI != "/v1/chat/completions?api-version=2026-10&probe=1" {
		t.Fatalf("Path = %q, RawURI = %q", reqs[0].Path, reqs[0].RawURI)
	}
}

func TestValidationErrors(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{})
	for name, tc := range map[string]struct {
		body   map[string]any
		status int
		code   any
	}{
		"未知模型":                  {chatBody(map[string]any{"model": "no-such-model"}), 404, "model_not_found"},
		"messages 为空":           {chatBody(map[string]any{"messages": []any{}}), 400, nil},
		"角色不对":                  {chatBody(map[string]any{"messages": []any{map[string]any{"role": "robot", "content": "x"}}}), 400, nil},
		"tool 消息缺 tool_call_id": {chatBody(map[string]any{"messages": []any{map[string]any{"role": "tool", "content": "x"}}}), 400, nil},
		"非流式带 stream_options":   {chatBody(map[string]any{"stream_options": map[string]any{"include_usage": true}}), 400, nil},
		"reasoning_effort 不对":   {chatBody(map[string]any{"reasoning_effort": "extreme"}), 400, nil},
		"json_schema 缺 schema":  {chatBody(map[string]any{"response_format": map[string]any{"type": "json_schema", "json_schema": map[string]any{"name": "x"}}}), 400, nil},
	} {
		resp, raw := post(t, s, "/chat/completions", key, tc.body)
		if resp.StatusCode != tc.status || at(decode(t, raw), "error", "code") != tc.code {
			t.Errorf("%s：status = %d, body = %s", name, resp.StatusCode, raw)
		}
	}
}

func TestAuthentication(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{APIKeys: []string{"sk-right"}})
	resp, raw := post(t, s, "/chat/completions", "", chatBody(nil))
	if resp.StatusCode != 401 || at(decode(t, raw), "error", "code") != "invalid_api_key" {
		t.Fatalf("缺 Key：%d %s", resp.StatusCode, raw)
	}
	resp, raw = post(t, s, "/chat/completions", key, chatBody(nil))
	if resp.StatusCode != 401 || strings.Contains(string(raw), "DO-NOT-LEAK") {
		t.Fatalf("Key 不对时应 401 且正文打码：%d %s", resp.StatusCode, raw)
	}
	echo := fakeopenai.Start(t, fakeopenai.Options{APIKeys: []string{"sk-right"}, EchoAuthorization: true})
	_, raw = post(t, echo, "/chat/completions", key, chatBody(nil))
	if !strings.Contains(string(raw), "Bearer "+key) {
		t.Fatalf("EchoAuthorization 时正文应回显 Authorization：%s", raw)
	}
}

// sse 读完一个 SSE 流，返回各 data 行（不含 [DONE]）与是否见到 [DONE]。
func sse(t *testing.T, s *fakeopenai.Server, body map[string]any) (events []map[string]any, done bool, err error) {
	t.Helper()
	raw, _ := json.Marshal(body)
	req, _ := http.NewRequestWithContext(t.Context(), http.MethodPost, s.URL+"/chat/completions", bytes.NewReader(raw))
	req.Header.Set("Authorization", "Bearer "+key)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, false, err
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("Content-Type = %q", ct)
	}
	sc := bufio.NewScanner(resp.Body)
	for sc.Scan() {
		line := sc.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		data := strings.TrimPrefix(line, "data: ")
		if data == "[DONE]" {
			done = true
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(data), &m); err != nil {
			t.Fatalf("分片不是 JSON：%s", data)
		}
		events = append(events, m)
	}
	return events, done, sc.Err()
}

func TestStreamWithUsage(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{SSEComments: true})
	s.ReplyNext(fakeopenai.Reply{Content: "一二三四五六七八九十", ReasoningContent: "数数", Usage: &fakeopenai.Usage{PromptTokens: 5, CompletionTokens: 7, ReasoningTokens: 2}})
	events, done, err := sse(t, s, chatBody(map[string]any{"stream": true, "stream_options": map[string]any{"include_usage": true}}))
	if err != nil || !done {
		t.Fatalf("err = %v, done = %v", err, done)
	}
	var text, thinking strings.Builder
	finish := ""
	for _, e := range events[:len(events)-1] {
		if _, ok := e["usage"]; !ok || e["usage"] != nil {
			t.Fatalf("include_usage 时其余分片的 usage 应为 null：%v", e)
		}
		if c, ok := at(e, "choices", 0, "delta", "content").(string); ok {
			text.WriteString(c)
		}
		if c, ok := at(e, "choices", 0, "delta", "reasoning_content").(string); ok {
			thinking.WriteString(c)
		}
		if f, ok := at(e, "choices", 0, "finish_reason").(string); ok {
			finish = f
		}
	}
	last := events[len(events)-1]
	if len(last["choices"].([]any)) != 0 || at(last, "usage", "completion_tokens") != float64(7) || at(last, "usage", "completion_tokens_details", "reasoning_tokens") != float64(2) {
		t.Fatalf("末尾分片 = %v", last)
	}
	if text.String() != "一二三四五六七八九十" || thinking.String() != "数数" || finish != "stop" {
		t.Fatalf("text = %q, thinking = %q, finish = %q", text.String(), thinking.String(), finish)
	}
}

func TestStreamWithoutUsageOption(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{})
	events, done, err := sse(t, s, chatBody(map[string]any{"stream": true}))
	if err != nil || !done {
		t.Fatalf("err = %v, done = %v", err, done)
	}
	for _, e := range events {
		if _, ok := e["usage"]; ok {
			t.Fatalf("没有 include_usage 时不应发 usage：%v", e)
		}
	}
}

func TestStreamToolCallFragments(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{})
	s.ReplyNext(fakeopenai.Reply{ToolCalls: []fakeopenai.ToolCall{{ID: "call_1", Name: "lookup", Arguments: `{"city":"上海","days":3}`}}})
	events, _, err := sse(t, s, chatBody(map[string]any{"stream": true}))
	if err != nil {
		t.Fatal(err)
	}
	var args strings.Builder
	id, name, finish, fragments := "", "", "", 0
	for _, e := range events {
		tc := at(e, "choices", 0, "delta", "tool_calls", 0)
		if tc != nil {
			fragments++
			if v, ok := at(tc, "id").(string); ok {
				id = v
			}
			if v, ok := at(tc, "function", "name").(string); ok {
				name = v
			}
			if v, ok := at(tc, "function", "arguments").(string); ok {
				args.WriteString(v)
			}
		}
		if f, ok := at(e, "choices", 0, "finish_reason").(string); ok {
			finish = f
		}
	}
	if id != "call_1" || name != "lookup" || args.String() != `{"city":"上海","days":3}` || finish != "tool_calls" || fragments < 3 {
		t.Fatalf("id = %q, name = %q, args = %q, finish = %q, fragments = %d", id, name, args.String(), finish, fragments)
	}
}

func TestStructuredOutputFollowsSchema(t *testing.T) {
	schema := json.RawMessage(`{
		"type":"object","required":["scores","label","meta"],"additionalProperties":false,
		"properties":{
			"scores":{"type":"array","items":{"type":"number","minimum":0,"maximum":1},"minItems":3,"maxItems":3},
			"label":{"enum":["refund","exchange"]},
			"meta":{"$ref":"#/$defs/meta"},
			"tags":{"type":"array","items":{"type":"string"},"minItems":2,"uniqueItems":true}
		},
		"$defs":{"meta":{"type":"object","properties":{"count":{"type":"integer","minimum":2},"ok":{"type":"boolean"},"at":{"type":"string","format":"date-time"}}}}
	}`)
	s := fakeopenai.Start(t, fakeopenai.Options{})
	_, raw := post(t, s, "/chat/completions", key, chatBody(map[string]any{
		"response_format": map[string]any{"type": "json_schema", "json_schema": map[string]any{"name": "out", "schema": schema, "strict": true}},
	}))
	content, _ := at(decode(t, raw), "choices", 0, "message", "content").(string)
	var out struct {
		Scores []float64 `json:"scores"`
		Label  string    `json:"label"`
		Meta   struct {
			Count int    `json:"count"`
			OK    bool   `json:"ok"`
			At    string `json:"at"`
		} `json:"meta"`
		Tags []string `json:"tags"`
	}
	if err := json.Unmarshal([]byte(content), &out); err != nil {
		t.Fatalf("content 不是 JSON：%q", content)
	}
	if len(out.Scores) != 3 || out.Scores[0] < 0 || out.Scores[0] > 1 || out.Label != "refund" || out.Meta.Count < 2 ||
		out.Meta.At == "" || len(out.Tags) != 2 || out.Tags[0] == out.Tags[1] {
		t.Fatalf("样例不符合 schema：%s", content)
	}
}

func TestEmbeddings(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{ReverseEmbeddings: true, EmbeddingDims: 4})
	resp, raw := post(t, s, "/embeddings", key, map[string]any{"model": fakeopenai.EmbeddingModel, "input": []string{"a", "b"}})
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d: %s", resp.StatusCode, raw)
	}
	m := decode(t, raw)
	if at(m, "data", 0, "index") != float64(1) || at(m, "data", 1, "index") != float64(0) || at(m, "usage", "prompt_tokens").(float64) <= 0 {
		t.Fatalf("ReverseEmbeddings 时 data 倒序、index 指向输入：%s", raw)
	}
	_, raw = post(t, s, "/embeddings", key, map[string]any{"model": fakeopenai.EmbeddingModel, "input": "a", "encoding_format": "base64"})
	b64, _ := at(decode(t, raw), "data", 0, "embedding").(string)
	buf, err := base64.StdEncoding.DecodeString(b64)
	if err != nil || len(buf) != 16 {
		t.Fatalf("base64 向量 = %q", b64)
	}
	got := make([]float32, 4)
	for i := range got {
		got[i] = math.Float32frombits(binary.LittleEndian.Uint32(buf[i*4:]))
	}
	if !slices.Equal(got, fakeopenai.Vector("a", 4)) {
		t.Fatalf("base64 解码 = %v", got)
	}
	resp, raw = post(t, s, "/embeddings", key, map[string]any{"model": "no-such-embedding", "input": "a"})
	if resp.StatusCode != 404 || at(decode(t, raw), "error", "code") != "model_not_found" {
		t.Fatalf("未知模型：%d %s", resp.StatusCode, raw)
	}
}

func TestModels(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{})
	req, _ := http.NewRequestWithContext(t.Context(), http.MethodGet, s.URL+"/models", nil)
	req.Header.Set("Authorization", "Bearer "+key)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	m := decode(t, raw)
	if len(m["data"].([]any)) != 2 {
		t.Fatalf("models = %s", raw)
	}
}

func TestFaults(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{})
	s.FailNext(fakeopenai.RateLimit("/chat/completions", "3"))
	resp, raw := post(t, s, "/embeddings", key, map[string]any{"model": fakeopenai.EmbeddingModel, "input": "a"})
	if resp.StatusCode != 200 {
		t.Fatalf("故障只匹配它的路径：%d %s", resp.StatusCode, raw)
	}
	resp, raw = post(t, s, "/chat/completions", key, chatBody(nil))
	if resp.StatusCode != 429 || resp.Header.Get("Retry-After") != "3" || at(decode(t, raw), "error", "code") != "rate_limit_exceeded" {
		t.Fatalf("429：%d %v %s", resp.StatusCode, resp.Header, raw)
	}
	if resp, _ := post(t, s, "/chat/completions", key, chatBody(nil)); resp.StatusCode != 200 {
		t.Fatalf("故障只生效一次：%d", resp.StatusCode)
	}

	s.FailNext(fakeopenai.Fault{Status: 401, Body: `{"error":{"message":"` + fakeopenai.AuthorizationPlaceholder + `"}}`})
	_, raw = post(t, s, "/chat/completions", key, chatBody(nil))
	if !strings.Contains(string(raw), "Bearer "+key) {
		t.Fatalf("Body 里的占位符应换成 Authorization：%s", raw)
	}

	s.FailNext(fakeopenai.Fault{MalformedJSON: true})
	resp, raw = post(t, s, "/chat/completions", key, chatBody(nil))
	if resp.StatusCode != 200 || json.Valid(raw) {
		t.Fatalf("MalformedJSON：%d %s", resp.StatusCode, raw)
	}

	s.FailNext(fakeopenai.Fault{Drop: true})
	rawBody, _ := json.Marshal(chatBody(nil))
	req, _ := http.NewRequestWithContext(t.Context(), http.MethodPost, s.URL+"/chat/completions", bytes.NewReader(rawBody))
	req.Header.Set("Authorization", "Bearer "+key)
	if resp, err := http.DefaultClient.Do(req); err == nil {
		resp.Body.Close()
		t.Fatal("Drop 时客户端应收到错误")
	}

	s.FailNext(fakeopenai.Fault{Delay: 5 * time.Second})
	ctx, cancel := context.WithTimeout(t.Context(), 200*time.Millisecond)
	defer cancel()
	req, _ = http.NewRequestWithContext(ctx, http.MethodPost, s.URL+"/chat/completions", bytes.NewReader(rawBody))
	req.Header.Set("Authorization", "Bearer "+key)
	start := time.Now()
	_, err := http.DefaultClient.Do(req)
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) > 3*time.Second {
		t.Fatalf("Delay：err = %v, elapsed = %v", err, time.Since(start))
	}

	s.ReplyNext(fakeopenai.Reply{Content: "很长的一段回复，会在中途断开"})
	s.FailNext(fakeopenai.Fault{CutStreamAfter: 2})
	events, done, err := sse(t, s, chatBody(map[string]any{"stream": true}))
	if done || err == nil || len(events) != 2 {
		t.Fatalf("CutStreamAfter：events = %d, done = %v, err = %v", len(events), done, err)
	}
}

func TestReplyNextIsUsedOnce(t *testing.T) {
	s := fakeopenai.Start(t, fakeopenai.Options{})
	s.ReplyNext(fakeopenai.Reply{Content: "指定的回复", FinishReason: "length", Usage: &fakeopenai.Usage{PromptTokens: 9, CompletionTokens: 4, CachedTokens: 8}})
	_, raw := post(t, s, "/chat/completions", key, chatBody(nil))
	m := decode(t, raw)
	if at(m, "choices", 0, "message", "content") != "指定的回复" || at(m, "choices", 0, "finish_reason") != "length" ||
		at(m, "usage", "prompt_tokens_details", "cached_tokens") != float64(8) {
		t.Fatalf("reply = %s", raw)
	}
	_, raw = post(t, s, "/chat/completions", key, chatBody(nil))
	if at(decode(t, raw), "choices", 0, "message", "content") != "fake reply: 你好" {
		t.Fatalf("指定的回复只用一次：%s", raw)
	}
	s.Reset()
	if len(s.Requests()) != 0 {
		t.Fatal("Reset 应清空记录")
	}
}
