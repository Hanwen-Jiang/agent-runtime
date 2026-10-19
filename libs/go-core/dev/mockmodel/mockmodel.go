// Package mockmodel 是模拟的 ModelProvider（开发实现）。语义见 providers/modelprovider。
//
// 输出是确定的：文本由输入推出，结构化输出按 schema 填入占位值，向量由文本哈希得到。只用于开发与测试。
package mockmodel

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"io"
	"strings"
	"sync"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

const (
	// GenerateModel 是可用于 Generate、StreamGenerate、Score 的模型名。
	GenerateModel = "mock-small"
	// EmbeddingModel 是可用于 Embed 的模型名。
	EmbeddingModel = "mock-embed"
	embeddingDims  = 8
)

// Call 是记录下的一次调用；平台作用域的 TenantID 为空串。
type Call struct {
	TenantID string
	Method   string
}

// Options 是构造参数。
type Options struct {
	Faults *testkit.Faults
}

// Provider 是模拟模型的工厂。
type Provider struct {
	mu     sync.Mutex
	faults *testkit.Faults
	calls  []Call
}

// New 构造一个模拟模型。
func New(opts Options) *Provider { return &Provider{faults: opts.Faults} }

func (p *Provider) Bind(t *commonv1.TenantContext) (modelprovider.Client, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &client{p: p, tenantID: s.TenantID}, nil
}

func (p *Provider) BindPlatform(purpose string) (modelprovider.Client, error) {
	if _, err := tenancy.PlatformScope(purpose); err != nil {
		return nil, err
	}
	return &client{p: p}, nil
}

func (p *Provider) Close() error { return nil }

// Calls 返回已记录的调用（开发专用）。
func (p *Provider) Calls() []Call {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]Call(nil), p.calls...)
}

type client struct {
	p        *Provider
	tenantID string
}

func (c *client) begin(method, model, want string) error {
	if err := c.p.faults.Check(method); err != nil {
		return err
	}
	if model != want {
		return providererr.New(providererr.NotFound, "unknown_model", "模型不存在："+model)
	}
	c.p.mu.Lock()
	c.p.calls = append(c.p.calls, Call{TenantID: c.tenantID, Method: method})
	c.p.mu.Unlock()
	return nil
}

func tokens(s string) int64 { return int64(len([]rune(s))/4 + 1) }

func usage(in, out string) *commonv1.Usage {
	return &commonv1.Usage{InputTokens: tokens(in), OutputTokens: tokens(out)}
}

// placeholder 按 JSON Schema 生成一个合法的占位值（只覆盖开发中用到的子集）。
func placeholder(schema map[string]any) any {
	switch schema["type"] {
	case "object":
		out := map[string]any{}
		props, _ := schema["properties"].(map[string]any)
		for name, sub := range props {
			if s, ok := sub.(map[string]any); ok {
				out[name] = placeholder(s)
			}
		}
		return out
	case "array":
		return []any{}
	case "integer", "number":
		return 0
	case "boolean":
		return false
	default:
		return "mock"
	}
}

func (c *client) generate(method string, req modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
	if err := c.begin(method, req.Model, GenerateModel); err != nil {
		return modelprovider.GenerateResult{}, err
	}
	if len(req.Messages) == 0 {
		return modelprovider.GenerateResult{}, providererr.New(providererr.Invalid, "invalid_request", "消息不能为空")
	}
	var input strings.Builder
	for _, m := range req.Messages {
		input.WriteString(m.Text)
	}
	last := req.Messages[len(req.Messages)-1].Text
	result := modelprovider.GenerateResult{Text: "mock reply: " + last, FinishReason: modelprovider.FinishStop}
	if len(req.OutputSchema) > 0 {
		var schema map[string]any
		if err := json.Unmarshal(req.OutputSchema, &schema); err != nil {
			return modelprovider.GenerateResult{}, providererr.New(providererr.Invalid, "invalid_schema", "OutputSchema 不是合法的 JSON")
		}
		result.Structured, _ = json.Marshal(placeholder(schema))
		result.Text = string(result.Structured)
	}
	result.Usage = usage(input.String(), result.Text)
	return result, nil
}

func (c *client) Generate(_ context.Context, req modelprovider.GenerateRequest) (modelprovider.GenerateResult, error) {
	return c.generate("Generate", req)
}

func (c *client) StreamGenerate(_ context.Context, req modelprovider.GenerateRequest) (modelprovider.Stream, error) {
	result, err := c.generate("StreamGenerate", req)
	if err != nil {
		return nil, err
	}
	text := []rune(result.Text)
	var chunks []modelprovider.Chunk
	for i := 0; i < len(text); i += 4 {
		chunks = append(chunks, modelprovider.Chunk{TextDelta: string(text[i:min(i+4, len(text))])})
	}
	final := result
	chunks = append(chunks, modelprovider.Chunk{Final: &final})
	return &stream{chunks: chunks}, nil
}

type stream struct {
	mu     sync.Mutex
	chunks []modelprovider.Chunk
}

func (s *stream) Recv(context.Context) (modelprovider.Chunk, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.chunks) == 0 {
		return modelprovider.Chunk{}, io.EOF
	}
	c := s.chunks[0]
	s.chunks = s.chunks[1:]
	return c, nil
}

func (s *stream) Close() error { return nil }

func (c *client) Score(_ context.Context, req modelprovider.ScoreRequest) (modelprovider.ScoreResult, error) {
	if err := c.begin("Score", req.Model, GenerateModel); err != nil {
		return modelprovider.ScoreResult{}, err
	}
	scores := make([]float64, len(req.Candidates))
	query := []rune(req.Query)
	for i, cand := range req.Candidates {
		shared := 0
		for _, r := range query {
			if strings.ContainsRune(cand, r) {
				shared++
			}
		}
		scores[i] = float64(shared) / float64(len(query)+1)
	}
	return modelprovider.ScoreResult{Scores: scores, Usage: usage(req.Query+strings.Join(req.Candidates, ""), "")}, nil
}

func (c *client) Embed(_ context.Context, req modelprovider.EmbedRequest) (modelprovider.EmbedResult, error) {
	if err := c.begin("Embed", req.Model, EmbeddingModel); err != nil {
		return modelprovider.EmbedResult{}, err
	}
	vectors := make([][]float32, len(req.Texts))
	for i, text := range req.Texts {
		sum := sha256.Sum256([]byte(text))
		v := make([]float32, embeddingDims)
		for d := range v {
			v[d] = float32(binary.BigEndian.Uint16(sum[d*2:])) / 65535
		}
		vectors[i] = v
	}
	return modelprovider.EmbedResult{Vectors: vectors, Usage: usage(strings.Join(req.Texts, ""), "")}, nil
}
