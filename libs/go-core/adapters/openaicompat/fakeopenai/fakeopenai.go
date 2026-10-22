// Package fakeopenai 是模拟的 OpenAI 兼容服务（基于 httptest），供 openaicompat 适配层与 Model Gateway 的测试复用
// （工作项“Model Gateway 单供应商直连”，docs/plan/model-gateway-direct.md）。只用于测试与开发，不是生产代码。
//
// 端点（Server.URL 形如 http://127.0.0.1:端口/v1）：
//   - POST /v1/chat/completions：非流式与 SSE 流式。流式请求带 stream_options.include_usage 时，末尾单独发一个
//     choices 为空、带 usage 的分片，其余分片的 usage 为 null，最后是 data: [DONE]；不带时不发 usage。
//     工具调用照 OpenAI 的格式返回（流式时参数分几片发出）；response_format 为 json_schema 时按 schema 生成合法 JSON（Sample）；
//     usage 带 prompt_tokens_details.cached_tokens 与 completion_tokens_details.reasoning_tokens；
//     请求带 reasoning_effort（或 Options.Reasoning）时消息附 reasoning_content，思考 token 计入 completion_tokens。
//   - POST /v1/embeddings：向量由文本哈希得到（Vector），每项带 index；支持 encoding_format 为 float 与 base64。
//   - GET /v1/models：列出可用模型。
//
// 请求校验照 OpenAI：JSON 不合法、缺 model、messages 为空、角色不对、tool 消息缺 tool_call_id、reasoning_effort 取值不对、
// 非流式请求带 stream_options、json_schema 缺 schema：400。未知模型：404，error.code 为 model_not_found。
// Authorization 缺失或 Key 不被认可：401 invalid_api_key，正文里的 Key 打码（Options.EchoAuthorization 时原样回显）。
//
// 每个请求都会记录（Requests）：路径、原始 URI（含查询串）、Authorization 头里的 Key、请求头、JSON 体。
// 故障按次注入（FailNext）：状态码与错误正文（例如 429 带 Retry-After、401、404、400、500）、延迟（触发客户端超时）、
// 200 但正文不是合法 JSON、直接断开连接、流式中途断开；可以在错误正文里回显 Authorization 头，用来检查密钥不外泄。
// 下一次 chat/completions 的回复可以指定（ReplyNext）：文本、思考内容、工具调用、finish_reason、usage。
package fakeopenai

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// ChatModel 是默认可用的生成模型名。
	ChatModel = "fake-chat"
	// EmbeddingModel 是默认可用的嵌入模型名。
	EmbeddingModel = "fake-embed"
	// DefaultEmbeddingDims 是默认的向量维度。
	DefaultEmbeddingDims = 8
	// AuthorizationPlaceholder 出现在 Fault.Body 里时替换成请求的 Authorization 头原文。
	AuthorizationPlaceholder = "{authorization}"

	created = 1760000000
)

// Options 是构造参数。
type Options struct {
	// APIKeys 是认可的 Key；为空时接受任何非空 Key。
	APIKeys []string
	// ChatModels 是可用的生成模型；为空时为 {ChatModel}。
	ChatModels []string
	// EmbeddingModels 是可用的嵌入模型；为空时为 {EmbeddingModel}。
	EmbeddingModels []string
	// EmbeddingDims 是向量维度；为 0 时为 DefaultEmbeddingDims。
	EmbeddingDims int
	// Reasoning 为 true 时每个回复都附 reasoning_content（不论请求是否带 reasoning_effort）。
	Reasoning bool
	// EchoAuthorization 为 true 时所有错误正文都原样回显请求的 Authorization 头。
	EchoAuthorization bool
	// SSEComments 为 true 时流式响应在数据分片之间插入 SSE 注释行（": keep-alive"），检查客户端能跳过它们。
	SSEComments bool
	// ReverseEmbeddings 为 true 时 embeddings 的 data 倒序返回（index 仍指向输入位置），检查客户端按 index 对回。
	ReverseEmbeddings bool
}

// Request 是记录下的一次请求。
type Request struct {
	Method string
	// Path 去掉了 /v1 前缀，例如 /chat/completions。
	Path string
	// RawURI 是请求行里的原始 URI（含 /v1 前缀与查询串），例如 /v1/chat/completions?api-version=1，
	// 用来检查密钥没有放进 URL。
	RawURI string
	// APIKey 是 Authorization: Bearer 之后的部分；没有该头时为空串。
	APIKey string
	Header http.Header
	Body   []byte
}

// JSON 把请求体解析成 map；不是 JSON 对象时返回 nil。
func (r Request) JSON() map[string]any {
	var m map[string]any
	if err := json.Unmarshal(r.Body, &m); err != nil {
		return nil
	}
	return m
}

// Fault 是按次注入的故障，被下一个匹配 Path 的请求取走。
type Fault struct {
	// Path 只匹配该路径（去掉 /v1，例如 /chat/completions）；空串匹配任何路径。
	Path string
	// Delay 在处理前等待；客户端先断开时不再写响应。只设 Delay 时，等待后照常处理。
	Delay time.Duration
	// Drop 为 true 时不写任何响应，直接断开连接。
	Drop bool
	// Status 非 0 时返回该状态码与错误正文。
	Status int
	// Body 是错误正文；为空时按 Status 生成 OpenAI 格式的错误（见 ErrorBody）。其中的 AuthorizationPlaceholder 换成 Authorization 头原文。
	Body string
	// Code 是生成错误正文时的 error.code（Body 为空时有效），例如 model_not_found、rate_limit_exceeded。
	Code string
	// Header 是附加的响应头，例如 Retry-After。
	Header map[string]string
	// EchoAuthorization 为 true 时生成的错误正文回显 Authorization 头原文。
	EchoAuthorization bool
	// MalformedJSON 为 true 时返回 200，正文不是合法 JSON。
	MalformedJSON bool
	// CutStreamAfter 大于 0 时，流式响应发出这么多个数据分片后断开连接（不发 [DONE]）。
	CutStreamAfter int
}

// RateLimit 构造一个 429 故障；retryAfter 非空时带 Retry-After 头。
func RateLimit(path, retryAfter string) Fault {
	f := Fault{Path: path, Status: http.StatusTooManyRequests, Code: "rate_limit_exceeded"}
	if retryAfter != "" {
		f.Header = map[string]string{"Retry-After": retryAfter}
	}
	return f
}

// ToolCall 是回复里的一次工具调用；Arguments 是 JSON 字符串。
type ToolCall struct {
	ID        string
	Name      string
	Arguments string
}

// Usage 是回复的计量；CachedTokens 计入 PromptTokens，ReasoningTokens 计入 CompletionTokens。
type Usage struct {
	PromptTokens     int
	CompletionTokens int
	CachedTokens     int
	ReasoningTokens  int
}

// Reply 指定下一次 chat/completions 的回复。
type Reply struct {
	// Content 是回复文本；为空且有 ToolCalls 时 content 为 null。
	Content          string
	ReasoningContent string
	ToolCalls        []ToolCall
	// FinishReason 为空时：有 ToolCalls 为 tool_calls，否则 stop。
	FinishReason string
	// Usage 为 nil 时按文本估算。
	Usage *Usage
}

// Server 是模拟的 OpenAI 兼容服务。
type Server struct {
	// URL 是 API 的根地址，带 /v1，例如 http://127.0.0.1:12345/v1。
	URL string

	opts     Options
	ts       *httptest.Server
	done     chan struct{}
	once     sync.Once
	mu       sync.Mutex
	requests []Request
	faults   []Fault
	replies  []Reply
	seq      int
}

// New 构造并启动一个模拟服务；用完调用 Close。
func New(opts Options) *Server {
	s := NewHandler(opts)
	s.ts = httptest.NewServer(s)
	s.URL = s.ts.URL + "/v1"
	return s
}

// Cleaner 是 testing.TB 的子集，免得非测试包引入 testing。
type Cleaner interface {
	Helper()
	Cleanup(func())
}

// Start 构造并启动一个模拟服务，在测试结束时关闭。
func Start(t Cleaner, opts Options) *Server {
	t.Helper()
	s := New(opts)
	t.Cleanup(s.Close)
	return s
}

// NewHandler 构造但不监听，供调用方自己挂到别的 http.Server 上（URL 为空）。
func NewHandler(opts Options) *Server {
	if len(opts.ChatModels) == 0 {
		opts.ChatModels = []string{ChatModel}
	}
	if len(opts.EmbeddingModels) == 0 {
		opts.EmbeddingModels = []string{EmbeddingModel}
	}
	if opts.EmbeddingDims <= 0 {
		opts.EmbeddingDims = DefaultEmbeddingDims
	}
	return &Server{opts: opts, done: make(chan struct{})}
}

// Close 停止服务；等待中的延迟故障立即结束。
func (s *Server) Close() {
	s.once.Do(func() { close(s.done) })
	if s.ts != nil {
		s.ts.CloseClientConnections()
		s.ts.Close()
	}
}

// Requests 返回已记录的全部请求（副本）。
func (s *Server) Requests() []Request {
	s.mu.Lock()
	defer s.mu.Unlock()
	return slices.Clone(s.requests)
}

// RequestsTo 返回发往 path（去掉 /v1）的请求。
func (s *Server) RequestsTo(path string) []Request {
	var out []Request
	for _, r := range s.Requests() {
		if r.Path == path {
			out = append(out, r)
		}
	}
	return out
}

// Reset 清空已记录的请求、未取走的故障与回复。
func (s *Server) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.requests, s.faults, s.replies = nil, nil, nil
}

// FailNext 安排一次故障，被下一个匹配 Path 的请求取走；多次调用依次排队。
func (s *Server) FailNext(f Fault) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.faults = append(s.faults, f)
}

// ReplyNext 指定下一次 chat/completions 的回复；多次调用依次排队。
func (s *Server) ReplyNext(r Reply) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.replies = append(s.replies, r)
}

func (s *Server) takeFault(path string) (Fault, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i, f := range s.faults {
		if f.Path == "" || f.Path == path {
			s.faults = slices.Delete(s.faults, i, i+1)
			return f, true
		}
	}
	return Fault{}, false
}

func (s *Server) takeReply() (Reply, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.replies) == 0 {
		return Reply{}, false
	}
	r := s.replies[0]
	s.replies = s.replies[1:]
	return r, true
}

func (s *Server) nextID(prefix string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.seq++
	return fmt.Sprintf("%s-fake-%d", prefix, s.seq)
}

// ErrorBody 生成 OpenAI 格式的错误正文；code 为空时 error.code 为 null。
func ErrorBody(typ, code, message string) string {
	var c any
	if code != "" {
		c = code
	}
	raw, _ := json.Marshal(map[string]any{"error": map[string]any{"message": message, "type": typ, "param": nil, "code": c}})
	return string(raw)
}

func errorType(status int) string {
	switch {
	case status == http.StatusTooManyRequests:
		return "requests"
	case status == http.StatusForbidden:
		return "permission_error"
	case status >= 500:
		return "server_error"
	default:
		return "invalid_request_error"
	}
}

// mask 照 OpenAI 的样子打码：保留前 6 位与后 4 位。
func mask(key string) string {
	if len(key) <= 10 {
		return "***"
	}
	return key[:6] + strings.Repeat("*", 4) + key[len(key)-4:]
}

type exchange struct {
	w    http.ResponseWriter
	r    *http.Request
	auth string
	echo bool
}

func (x exchange) writeJSON(status int, v any) {
	raw, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	x.w.Header().Set("Content-Type", "application/json")
	x.w.WriteHeader(status)
	_, _ = x.w.Write(raw)
}

func (x exchange) writeError(status int, code, message string) {
	if x.echo {
		message += "（Authorization: " + x.auth + "）"
	}
	x.w.Header().Set("Content-Type", "application/json")
	x.w.WriteHeader(status)
	_, _ = io.WriteString(x.w, ErrorBody(errorType(status), code, message))
}

// ServeHTTP 处理一个请求。
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	path := strings.TrimPrefix(r.URL.Path, "/v1")
	auth := r.Header.Get("Authorization")
	key := ""
	if strings.HasPrefix(auth, "Bearer ") {
		key = strings.TrimSpace(strings.TrimPrefix(auth, "Bearer "))
	}
	s.mu.Lock()
	s.requests = append(s.requests, Request{Method: r.Method, Path: path, RawURI: r.RequestURI, APIKey: key, Header: r.Header.Clone(), Body: body})
	s.mu.Unlock()

	x := exchange{w: w, r: r, auth: auth, echo: s.opts.EchoAuthorization}
	fault, hasFault := s.takeFault(path)
	if hasFault {
		if fault.EchoAuthorization {
			x.echo = true
		}
		if handled := s.applyFault(x, fault); handled {
			return
		}
	}

	if !strings.HasPrefix(r.URL.Path, "/v1/") {
		x.writeError(http.StatusNotFound, "", "Invalid URL ("+r.Method+" "+r.URL.Path+")")
		return
	}
	if key == "" {
		x.writeError(http.StatusUnauthorized, "invalid_api_key", "You didn't provide an API key.")
		return
	}
	if len(s.opts.APIKeys) > 0 && !slices.Contains(s.opts.APIKeys, key) {
		x.writeError(http.StatusUnauthorized, "invalid_api_key", "Incorrect API key provided: "+mask(key)+".")
		return
	}
	switch {
	case path == "/chat/completions" && r.Method == http.MethodPost:
		s.chat(x, body, fault.CutStreamAfter)
	case path == "/embeddings" && r.Method == http.MethodPost:
		s.embeddings(x, body)
	case path == "/models" && r.Method == http.MethodGet:
		s.models(x)
	default:
		x.writeError(http.StatusNotFound, "", "Invalid URL ("+r.Method+" "+r.URL.Path+")")
	}
}

// applyFault 执行故障；返回 true 表示响应已经处理完。
func (s *Server) applyFault(x exchange, f Fault) bool {
	if f.Delay > 0 {
		timer := time.NewTimer(f.Delay)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-x.r.Context().Done():
			return true
		case <-s.done:
			return true
		}
	}
	if f.Drop {
		panic(http.ErrAbortHandler)
	}
	for k, v := range f.Header {
		x.w.Header().Set(k, v)
	}
	switch {
	case f.Status != 0:
		if f.Body != "" {
			x.w.Header().Set("Content-Type", "application/json")
			x.w.WriteHeader(f.Status)
			_, _ = io.WriteString(x.w, strings.ReplaceAll(f.Body, AuthorizationPlaceholder, x.auth))
			return true
		}
		x.writeError(f.Status, f.Code, fmt.Sprintf("fake upstream error %d", f.Status))
		return true
	case f.MalformedJSON:
		x.w.Header().Set("Content-Type", "application/json")
		x.w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(x.w, `{"id":"chatcmpl-broken","object":"chat.completion","choices":[{"index":0,"message":{"role":"assist`)
		return true
	}
	return false
}

func (s *Server) models(x exchange) {
	var data []map[string]any
	for _, m := range slices.Concat(s.opts.ChatModels, s.opts.EmbeddingModels) {
		data = append(data, map[string]any{"id": m, "object": "model", "created": created, "owned_by": "fake"})
	}
	x.writeJSON(http.StatusOK, map[string]any{"object": "list", "data": data})
}

// ---- chat/completions ----

type wireToolCall struct {
	Index    *int   `json:"index,omitempty"`
	ID       string `json:"id,omitempty"`
	Type     string `json:"type,omitempty"`
	Function struct {
		Name      string `json:"name,omitempty"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}

type chatMessage struct {
	Role       string          `json:"role"`
	Content    json.RawMessage `json:"content"`
	ToolCalls  []wireToolCall  `json:"tool_calls"`
	ToolCallID string          `json:"tool_call_id"`
}

type chatRequest struct {
	Model         string        `json:"model"`
	Messages      []chatMessage `json:"messages"`
	Stream        bool          `json:"stream"`
	StreamOptions *struct {
		IncludeUsage bool `json:"include_usage"`
	} `json:"stream_options"`
	ResponseFormat *struct {
		Type       string `json:"type"`
		JSONSchema *struct {
			Name   string          `json:"name"`
			Schema json.RawMessage `json:"schema"`
			Strict *bool           `json:"strict"`
		} `json:"json_schema"`
	} `json:"response_format"`
	Tools           []json.RawMessage `json:"tools"`
	ReasoningEffort string            `json:"reasoning_effort"`
}

// contentText 取消息的文本：字符串，或内容片段数组里 type 为 text 的部分。
func contentText(raw json.RawMessage) (string, bool) {
	if len(raw) == 0 || string(raw) == "null" {
		return "", true
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s, true
	}
	var parts []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal(raw, &parts); err != nil {
		return "", false
	}
	var b strings.Builder
	for _, p := range parts {
		if p.Type == "text" {
			b.WriteString(p.Text)
		}
	}
	return b.String(), true
}

// Tokens 是估算 token 数的规则：每 4 个字符 1 个，至少 1 个；空串为 0。
func Tokens(s string) int {
	if s == "" {
		return 0
	}
	return len([]rune(s))/4 + 1
}

func (s *Server) chat(x exchange, body []byte, cutAfter int) {
	var req chatRequest
	if err := json.Unmarshal(body, &req); err != nil {
		x.writeError(http.StatusBadRequest, "", "We could not parse the JSON body of your request.")
		return
	}
	if req.Model == "" {
		x.writeError(http.StatusBadRequest, "", "you must provide a model parameter")
		return
	}
	if len(req.Messages) == 0 {
		x.writeError(http.StatusBadRequest, "", "[] is too short - 'messages'")
		return
	}
	var prompt strings.Builder
	lastUser := ""
	for i, m := range req.Messages {
		text, ok := contentText(m.Content)
		if !ok {
			x.writeError(http.StatusBadRequest, "", fmt.Sprintf("Invalid content in messages[%d].", i))
			return
		}
		switch m.Role {
		case "system", "developer", "assistant":
		case "user":
			lastUser = text
		case "tool":
			if m.ToolCallID == "" {
				x.writeError(http.StatusBadRequest, "", fmt.Sprintf("Missing required parameter: 'messages[%d].tool_call_id'.", i))
				return
			}
		default:
			x.writeError(http.StatusBadRequest, "", fmt.Sprintf("Invalid value: '%s'. Supported values are: 'system', 'assistant', 'user', 'tool', and 'developer'.", m.Role))
			return
		}
		prompt.WriteString(text)
		for _, tc := range m.ToolCalls {
			prompt.WriteString(tc.Function.Name + tc.Function.Arguments)
		}
	}
	for _, t := range req.Tools {
		prompt.Write(t)
	}
	switch req.ReasoningEffort {
	case "", "minimal", "low", "medium", "high":
	default:
		x.writeError(http.StatusBadRequest, "", "Invalid value: '"+req.ReasoningEffort+"'. Supported values are: 'minimal', 'low', 'medium', and 'high'.")
		return
	}
	if req.StreamOptions != nil && !req.Stream {
		x.writeError(http.StatusBadRequest, "", "The 'stream_options' parameter is only allowed when 'stream' is enabled.")
		return
	}
	var schema json.RawMessage
	if rf := req.ResponseFormat; rf != nil {
		switch rf.Type {
		case "text", "json_object":
		case "json_schema":
			if rf.JSONSchema == nil || len(rf.JSONSchema.Schema) == 0 || rf.JSONSchema.Name == "" {
				x.writeError(http.StatusBadRequest, "", "Missing required parameter: 'response_format.json_schema'.")
				return
			}
			schema = rf.JSONSchema.Schema
		default:
			x.writeError(http.StatusBadRequest, "", "Invalid value: '"+rf.Type+"'. Supported values are: 'json_object', 'json_schema', and 'text'.")
			return
		}
	}
	if !slices.Contains(s.opts.ChatModels, req.Model) {
		x.writeError(http.StatusNotFound, "model_not_found", "The model `"+req.Model+"` does not exist or you do not have access to it.")
		return
	}

	reply, scripted := s.takeReply()
	if !scripted {
		reply = Reply{Content: "fake reply: " + lastUser}
		if schema != nil {
			sample, err := Sample(schema)
			if err != nil {
				x.writeError(http.StatusBadRequest, "", "Invalid schema for response_format: "+err.Error())
				return
			}
			reply.Content = string(sample)
		}
		if req.ReasoningEffort != "" || s.opts.Reasoning {
			reply.ReasoningContent = "fake reasoning (effort=" + req.ReasoningEffort + ")：先读题，再作答。"
		}
	}
	if reply.FinishReason == "" {
		reply.FinishReason = "stop"
		if len(reply.ToolCalls) > 0 {
			reply.FinishReason = "tool_calls"
		}
	}
	usage := reply.Usage
	if usage == nil {
		out := Tokens(reply.Content)
		for _, tc := range reply.ToolCalls {
			out += Tokens(tc.Name + tc.Arguments)
		}
		reasoning := Tokens(reply.ReasoningContent)
		usage = &Usage{PromptTokens: max(Tokens(prompt.String()), 1), CompletionTokens: max(out+reasoning, 1), ReasoningTokens: reasoning}
	}
	id := s.nextID("chatcmpl")
	if req.Stream {
		includeUsage := req.StreamOptions != nil && req.StreamOptions.IncludeUsage
		s.stream(x, id, req.Model, reply, usage, includeUsage, cutAfter)
		return
	}
	msg := map[string]any{"role": "assistant", "content": reply.Content}
	if reply.Content == "" && len(reply.ToolCalls) > 0 {
		msg["content"] = nil
	}
	if reply.ReasoningContent != "" {
		msg["reasoning_content"] = reply.ReasoningContent
	}
	if len(reply.ToolCalls) > 0 {
		var calls []wireToolCall
		for _, tc := range reply.ToolCalls {
			w := wireToolCall{ID: tc.ID, Type: "function"}
			w.Function.Name, w.Function.Arguments = tc.Name, tc.Arguments
			calls = append(calls, w)
		}
		msg["tool_calls"] = calls
	}
	x.writeJSON(http.StatusOK, map[string]any{
		"id": id, "object": "chat.completion", "created": created, "model": req.Model, "system_fingerprint": "fp_fake",
		"choices": []any{map[string]any{"index": 0, "message": msg, "finish_reason": reply.FinishReason, "logprobs": nil}},
		"usage":   usageJSON(usage),
	})
}

func usageJSON(u *Usage) map[string]any {
	return map[string]any{
		"prompt_tokens": u.PromptTokens, "completion_tokens": u.CompletionTokens, "total_tokens": u.PromptTokens + u.CompletionTokens,
		"prompt_tokens_details":     map[string]any{"cached_tokens": u.CachedTokens, "audio_tokens": 0},
		"completion_tokens_details": map[string]any{"reasoning_tokens": u.ReasoningTokens, "audio_tokens": 0},
	}
}

// pieces 把文本按每 n 个字符切开。
func pieces(s string, n int) []string {
	r := []rune(s)
	var out []string
	for i := 0; i < len(r); i += n {
		out = append(out, string(r[i:min(i+n, len(r))]))
	}
	return out
}

func (s *Server) stream(x exchange, id, model string, reply Reply, usage *Usage, includeUsage bool, cutAfter int) {
	var events []map[string]any
	chunk := func(delta map[string]any, finish any) map[string]any {
		c := map[string]any{
			"id": id, "object": "chat.completion.chunk", "created": created, "model": model, "system_fingerprint": "fp_fake",
			"choices": []any{map[string]any{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": nil}},
		}
		if includeUsage {
			c["usage"] = nil
		}
		return c
	}
	events = append(events, chunk(map[string]any{"role": "assistant", "content": ""}, nil))
	for _, p := range pieces(reply.ReasoningContent, 4) {
		events = append(events, chunk(map[string]any{"reasoning_content": p}, nil))
	}
	for _, p := range pieces(reply.Content, 4) {
		events = append(events, chunk(map[string]any{"content": p}, nil))
	}
	for i, tc := range reply.ToolCalls {
		head := wireToolCall{Index: &i, ID: tc.ID, Type: "function"}
		head.Function.Name = tc.Name
		events = append(events, chunk(map[string]any{"tool_calls": []wireToolCall{head}}, nil))
		for _, p := range pieces(tc.Arguments, 5) {
			part := wireToolCall{Index: &i}
			part.Function.Arguments = p
			events = append(events, chunk(map[string]any{"tool_calls": []wireToolCall{part}}, nil))
		}
	}
	events = append(events, chunk(map[string]any{}, reply.FinishReason))
	if includeUsage {
		events = append(events, map[string]any{
			"id": id, "object": "chat.completion.chunk", "created": created, "model": model, "system_fingerprint": "fp_fake",
			"choices": []any{}, "usage": usageJSON(usage),
		})
	}

	h := x.w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	x.w.WriteHeader(http.StatusOK)
	flusher, _ := x.w.(http.Flusher)
	flush := func() {
		if flusher != nil {
			flusher.Flush()
		}
	}
	for n, e := range events {
		if cutAfter > 0 && n == cutAfter {
			flush()
			panic(http.ErrAbortHandler)
		}
		if s.opts.SSEComments {
			_, _ = io.WriteString(x.w, ": keep-alive\n\n")
		}
		raw, _ := json.Marshal(e)
		_, _ = fmt.Fprintf(x.w, "data: %s\n\n", raw)
		flush()
	}
	if cutAfter > 0 && cutAfter >= len(events) {
		panic(http.ErrAbortHandler)
	}
	_, _ = io.WriteString(x.w, "data: [DONE]\n\n")
	flush()
}

// ---- embeddings ----

// Vector 是文本对应的确定向量（各分量在 [0, 1] 内）。
func Vector(text string, dims int) []float32 {
	v := make([]float32, dims)
	for d := range v {
		sum := sha256.Sum256([]byte(strconv.Itoa(d/16) + "\x00" + text))
		v[d] = float32(binary.BigEndian.Uint16(sum[(d%16)*2:])) / 65535
	}
	return v
}

func (s *Server) embeddings(x exchange, body []byte) {
	var req struct {
		Model          string          `json:"model"`
		Input          json.RawMessage `json:"input"`
		EncodingFormat string          `json:"encoding_format"`
	}
	if err := json.Unmarshal(body, &req); err != nil {
		x.writeError(http.StatusBadRequest, "", "We could not parse the JSON body of your request.")
		return
	}
	if req.Model == "" {
		x.writeError(http.StatusBadRequest, "", "you must provide a model parameter")
		return
	}
	var texts []string
	var single string
	if err := json.Unmarshal(req.Input, &single); err == nil {
		texts = []string{single}
	} else if err := json.Unmarshal(req.Input, &texts); err != nil {
		x.writeError(http.StatusBadRequest, "", "'$.input' is invalid. Please check the API reference.")
		return
	}
	if len(texts) == 0 || slices.Contains(texts, "") {
		x.writeError(http.StatusBadRequest, "", "'$.input' is invalid. Please check the API reference.")
		return
	}
	switch req.EncodingFormat {
	case "", "float", "base64":
	default:
		x.writeError(http.StatusBadRequest, "", "Invalid value: '"+req.EncodingFormat+"'. Supported values are: 'float' and 'base64'.")
		return
	}
	if !slices.Contains(s.opts.EmbeddingModels, req.Model) {
		x.writeError(http.StatusNotFound, "model_not_found", "The model `"+req.Model+"` does not exist or you do not have access to it.")
		return
	}
	data := make([]map[string]any, len(texts))
	tokens := 0
	for i, t := range texts {
		vec := Vector(t, s.opts.EmbeddingDims)
		var emb any = vec
		if req.EncodingFormat == "base64" {
			buf := make([]byte, 4*len(vec))
			for j, f := range vec {
				binary.LittleEndian.PutUint32(buf[j*4:], math.Float32bits(f))
			}
			emb = base64.StdEncoding.EncodeToString(buf)
		}
		data[i] = map[string]any{"object": "embedding", "index": i, "embedding": emb}
		tokens += max(Tokens(t), 1)
	}
	if s.opts.ReverseEmbeddings {
		slices.Reverse(data)
	}
	x.writeJSON(http.StatusOK, map[string]any{
		"object": "list", "data": data, "model": req.Model,
		"usage": map[string]any{"prompt_tokens": tokens, "total_tokens": tokens},
	})
}

// ---- 按 schema 生成样例 ----

// Sample 按 JSON Schema 生成一个合法的样例值（覆盖结构化输出常用的子集：type、properties、required、items、
// minItems、enum、const、anyOf、oneOf、allOf、本文件内的 $ref、minimum、maximum、minLength、format）。
// 数组按 minItems 生成元素个数。
func Sample(schema json.RawMessage) (json.RawMessage, error) {
	var root any
	dec := json.NewDecoder(bytes.NewReader(schema))
	dec.UseNumber()
	if err := dec.Decode(&root); err != nil {
		return nil, fmt.Errorf("schema 不是合法的 JSON：%w", err)
	}
	m, ok := root.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("schema 必须是 JSON 对象")
	}
	return json.Marshal(sampleOf(m, m, 0))
}

func num(v any) (float64, bool) {
	switch n := v.(type) {
	case json.Number:
		f, err := n.Float64()
		return f, err == nil
	case float64:
		return n, true
	}
	return 0, false
}

func resolve(root map[string]any, ref string) map[string]any {
	if !strings.HasPrefix(ref, "#/") {
		return map[string]any{}
	}
	var cur any = root
	for _, part := range strings.Split(strings.TrimPrefix(ref, "#/"), "/") {
		part = strings.ReplaceAll(strings.ReplaceAll(part, "~1", "/"), "~0", "~")
		m, ok := cur.(map[string]any)
		if !ok {
			return map[string]any{}
		}
		cur = m[part]
	}
	if m, ok := cur.(map[string]any); ok {
		return m
	}
	return map[string]any{}
}

func sampleOf(root, s map[string]any, depth int) any {
	if depth > 32 {
		return nil
	}
	if ref, ok := s["$ref"].(string); ok {
		return sampleOf(root, resolve(root, ref), depth+1)
	}
	if v, ok := s["const"]; ok {
		return v
	}
	if e, ok := s["enum"].([]any); ok && len(e) > 0 {
		return e[0]
	}
	if all, ok := s["allOf"].([]any); ok && len(all) > 0 {
		merged := map[string]any{}
		props := map[string]any{}
		for _, part := range append([]any{s}, all...) {
			p, _ := part.(map[string]any)
			if r, ok := p["$ref"].(string); ok {
				p = resolve(root, r)
			}
			for k, v := range p {
				if k != "allOf" && k != "properties" {
					merged[k] = v
				}
			}
			if pp, ok := p["properties"].(map[string]any); ok {
				for k, v := range pp {
					props[k] = v
				}
			}
		}
		if len(props) > 0 {
			merged["properties"] = props
		}
		return sampleOf(root, merged, depth+1)
	}
	for _, k := range []string{"anyOf", "oneOf"} {
		if alts, ok := s[k].([]any); ok && len(alts) > 0 {
			for _, alt := range alts {
				if m, ok := alt.(map[string]any); ok && m["type"] != "null" {
					return sampleOf(root, m, depth+1)
				}
			}
			m, _ := alts[0].(map[string]any)
			return sampleOf(root, m, depth+1)
		}
	}
	typ, _ := s["type"].(string)
	if types, ok := s["type"].([]any); ok {
		for _, t := range types {
			if ts, _ := t.(string); ts != "null" {
				typ = ts
				break
			}
		}
	}
	if typ == "" {
		if _, ok := s["properties"]; ok {
			typ = "object"
		} else if _, ok := s["items"]; ok {
			typ = "array"
		}
	}
	switch typ {
	case "object":
		out := map[string]any{}
		if props, ok := s["properties"].(map[string]any); ok {
			for name, sub := range props {
				if m, ok := sub.(map[string]any); ok {
					out[name] = sampleOf(root, m, depth+1)
				}
			}
		}
		return out
	case "array":
		n := 0
		if f, ok := num(s["minItems"]); ok {
			n = int(f)
		}
		items, _ := s["items"].(map[string]any)
		if ref, ok := items["$ref"].(string); ok {
			items = resolve(root, ref)
		}
		unique, _ := s["uniqueItems"].(bool)
		enum, _ := items["enum"].([]any)
		out := make([]any, n)
		for i := range out {
			v := sampleOf(root, items, depth+1)
			if unique && len(enum) > 0 {
				v = enum[i%len(enum)] // uniqueItems 时元素互不相同
			} else if str, ok := v.(string); ok && unique {
				v = str + "-" + strconv.Itoa(i)
			}
			out[i] = v
		}
		return out
	case "integer", "number":
		lo, hasLo := num(s["minimum"])
		hi, hasHi := num(s["maximum"])
		if f, ok := num(s["exclusiveMinimum"]); ok {
			lo, hasLo = f+1, true
		}
		if f, ok := num(s["exclusiveMaximum"]); ok {
			hi, hasHi = f-1, true
		}
		v := 0.5
		if typ == "integer" {
			v = 1
		}
		switch {
		case hasLo && hasHi:
			v = (lo + hi) / 2
		case hasLo && v < lo:
			v = lo
		case hasHi && v > hi:
			v = hi
		}
		if typ == "integer" {
			return int64(math.Ceil(v))
		}
		return v
	case "boolean":
		return true
	case "null":
		return nil
	default:
		v := "fake"
		switch s["format"] {
		case "date-time":
			v = "2026-10-21T00:00:00Z"
		case "date":
			v = "2026-10-21"
		case "uri":
			v = "https://example.invalid/fake"
		case "email":
			v = "fake@example.invalid"
		case "uuid":
			v = "00000000-0000-4000-8000-000000000000"
		}
		if f, ok := num(s["minLength"]); ok && len([]rune(v)) < int(f) {
			v += strings.Repeat("x", int(f)-len([]rune(v)))
		}
		return v
	}
}
