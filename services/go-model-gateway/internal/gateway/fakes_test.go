package gateway_test

// 测试支撑：可编排的假供应商（modelprovider.Provider）。按模型名注入失败序列，记录 Bind 的租户与每次请求。

import (
	"context"
	"io"
	"sync"

	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// fakeCall 是记录下的一次供应商调用。
type fakeCall struct {
	// TenantID 是客户端绑定的租户；平台作用域为空串。
	TenantID string
	Method   string
	Model    string
	Generate modelprovider.GenerateRequest
	Score    modelprovider.ScoreRequest
	Embed    modelprovider.EmbedRequest
}

type generateFunc func(modelprovider.GenerateRequest) (modelprovider.GenerateResult, error)

type fakeProvider struct {
	mu            sync.Mutex
	binds         []string
	platformBinds []string
	calls         []fakeCall
	failures      map[string][]error
	generate      map[string]generateFunc
	usage         *commonv1.Usage
}

func newFakeProvider() *fakeProvider {
	return &fakeProvider{
		failures: map[string][]error{},
		generate: map[string]generateFunc{},
		// 默认 Usage：按 mid-a 的单价（0.15/0.6/0.075）成本为 (1000-200)*0.15 + 200*0.075 + 500*0.6 = 435。
		usage: &commonv1.Usage{InputTokens: 1000, OutputTokens: 500, CacheReadTokens: 200, ReasoningTokens: 50},
	}
}

// failNext 让 model 接下来的调用依次返回 errs（不分方法），用完后恢复正常。
func (p *fakeProvider) failNext(model string, errs ...error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.failures[model] = append(p.failures[model], errs...)
}

// onGenerate 设定 model 的 Generate 与 StreamGenerate 结果；返回的 Usage 为空时用默认 Usage。
func (p *fakeProvider) onGenerate(model string, fn generateFunc) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.generate[model] = fn
}

// setUsage 设定之后各次调用返回的 Usage。
func (p *fakeProvider) setUsage(u *commonv1.Usage) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.usage = u
}

func (p *fakeProvider) allCalls() []fakeCall {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]fakeCall(nil), p.calls...)
}

func (p *fakeProvider) callsTo(model string) int {
	n := 0
	for _, c := range p.allCalls() {
		if c.Model == model {
			n++
		}
	}
	return n
}

// boundTenants 返回 Bind 过的租户与 BindPlatform 过的用途。
func (p *fakeProvider) boundTenants() (tenants, platform []string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.binds...), append([]string(nil), p.platformBinds...)
}

func (p *fakeProvider) Bind(t *commonv1.TenantContext) (modelprovider.Client, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.binds = append(p.binds, s.TenantID)
	return &fakeClient{p: p, tenantID: s.TenantID}, nil
}

func (p *fakeProvider) BindPlatform(purpose string) (modelprovider.Client, error) {
	if _, err := tenancy.PlatformScope(purpose); err != nil {
		return nil, err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.platformBinds = append(p.platformBinds, purpose)
	return &fakeClient{p: p}, nil
}

func (p *fakeProvider) Close() error { return nil }

// begin 记录调用并取出为该模型安排的失败。
func (p *fakeProvider) begin(call fakeCall) (*commonv1.Usage, generateFunc, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.calls = append(p.calls, call)
	usage := proto.Clone(p.usage).(*commonv1.Usage)
	if queue := p.failures[call.Model]; len(queue) > 0 {
		p.failures[call.Model] = queue[1:]
		return nil, nil, queue[0]
	}
	return usage, p.generate[call.Model], nil
}

type fakeClient struct {
	p        *fakeProvider
	tenantID string
}

func (c *fakeClient) result(method string, req modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
	usage, fn, err := c.p.begin(fakeCall{TenantID: c.tenantID, Method: method, Model: req.Model, Generate: req})
	if err != nil {
		return modelprovider.GenerateResult{}, err
	}
	if fn == nil {
		return modelprovider.GenerateResult{Text: "reply from " + req.Model, FinishReason: modelprovider.FinishStop, Usage: usage}, nil
	}
	r, err := fn(req)
	if err == nil && r.Usage == nil {
		r.Usage = usage
	}
	if err == nil && r.FinishReason == "" {
		r.FinishReason = modelprovider.FinishStop
	}
	return r, err
}

func (c *fakeClient) Generate(_ context.Context, req modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
	return c.result("Generate", req)
}

// StreamGenerate 先发思考分片，再把文本切成 3 个字符一片，最后一个分片带完整结果。
func (c *fakeClient) StreamGenerate(_ context.Context, req modelprovider.GenerateRequest) (modelprovider.Stream, error) {
	r, err := c.result("StreamGenerate", req)
	if err != nil {
		return nil, err
	}
	var chunks []modelprovider.Chunk
	if r.Thinking != "" {
		chunks = append(chunks, modelprovider.Chunk{ThinkingDelta: r.Thinking})
	}
	text := []rune(r.Text)
	for i := 0; i < len(text); i += 3 {
		chunks = append(chunks, modelprovider.Chunk{TextDelta: string(text[i:min(i+3, len(text))])})
	}
	for i := range r.ToolCalls {
		chunks = append(chunks, modelprovider.Chunk{ToolCall: &r.ToolCalls[i]})
	}
	final := r
	chunks = append(chunks, modelprovider.Chunk{Final: &final})
	return &fakeStream{chunks: chunks}, nil
}

type fakeStream struct {
	mu     sync.Mutex
	chunks []modelprovider.Chunk
}

func (s *fakeStream) Recv(context.Context) (modelprovider.Chunk, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.chunks) == 0 {
		return modelprovider.Chunk{}, io.EOF
	}
	c := s.chunks[0]
	s.chunks = s.chunks[1:]
	return c, nil
}

func (s *fakeStream) Close() error { return nil }

// Score 依次给 0.1、0.2、0.3……
func (c *fakeClient) Score(_ context.Context, req modelprovider.ScoreRequest) (modelprovider.ScoreResult, error) {
	usage, _, err := c.p.begin(fakeCall{TenantID: c.tenantID, Method: "Score", Model: req.Model, Score: req})
	if err != nil {
		return modelprovider.ScoreResult{}, err
	}
	scores := make([]float64, len(req.Candidates))
	for i := range scores {
		scores[i] = float64(i+1) / 10
	}
	return modelprovider.ScoreResult{Scores: scores, Usage: usage}, nil
}

// Embed 的向量是 [文本的字节数, 1]。
func (c *fakeClient) Embed(_ context.Context, req modelprovider.EmbedRequest) (modelprovider.EmbedResult, error) {
	usage, _, err := c.p.begin(fakeCall{TenantID: c.tenantID, Method: "Embed", Model: req.Model, Embed: req})
	if err != nil {
		return modelprovider.EmbedResult{}, err
	}
	vectors := make([][]float32, len(req.Texts))
	for i, text := range req.Texts {
		vectors[i] = []float32{float32(len(text)), 1}
	}
	return modelprovider.EmbedResult{Vectors: vectors, Usage: usage}, nil
}
