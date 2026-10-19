// Package modelprovidertest 是 ModelProvider 的一致性测试。只在 Go 侧有。
//
// 用例只做宽松断言（非空、对应关系、计量、schema），供应商适配器与模拟模型都能通过。
package modelprovidertest

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
)

// RecordedCall 是 harness 记录下的一次供应商调用。
type RecordedCall struct {
	// TenantID 是用来选凭据与计量的作用域；平台作用域为空串。
	TenantID string
	Method   string
}

// Harness 由实现所在包提供。
type Harness interface {
	Provider() modelprovider.Provider
	conformance.Faultable
	// Model 是能用于测试的生成模型；生产适配器用最便宜的模型。
	Model() string
	EmbeddingModel() string
	// Calls 返回已记录的调用；做不到时返回 conformance.ErrUnsupported。
	Calls() ([]RecordedCall, error)
}

var outputSchema = json.RawMessage(`{"type":"object","required":["answer"],"properties":{"answer":{"type":"string"}},"additionalProperties":false}`)

func ask(h Harness, text string) modelprovider.GenerateRequest {
	return modelprovider.GenerateRequest{
		Model:           h.Model(),
		Messages:        []modelprovider.Message{{Role: modelprovider.RoleUser, Text: text}},
		MaxOutputTokens: 64,
	}
}

// Run 跑完整的 ModelProvider 一致性测试。
func Run(t *testing.T, newHarness func(t *testing.T) Harness) {
	conformance.RunBindingRules(t, func(t *testing.T) conformance.Binder[modelprovider.Client] { return newHarness(t).Provider() })
	ctx := context.Background()
	setup := func(t *testing.T) (Harness, modelprovider.Client) {
		h := newHarness(t)
		return h, conformance.Must(h.Provider().Bind(conformance.TenantA()))
	}

	conformance.Case(t, "GenerateReturnsTextAndUsage", func(t *testing.T) {
		h, c := setup(t)
		r := conformance.Must(c.Generate(ctx, ask(h, "用一个词回答：天空是什么颜色？")))
		if strings.TrimSpace(r.Text) == "" || r.Usage == nil || r.Usage.GetInputTokens() <= 0 || r.Usage.GetOutputTokens() <= 0 {
			t.Fatalf("result = %+v", r)
		}
	})
	conformance.Case(t, "StructuredOutputMatchesSchema", func(t *testing.T) {
		h, c := setup(t)
		req := ask(h, "回答 answer 字段：1+1 等于几？")
		req.OutputSchema = outputSchema
		r := conformance.Must(c.Generate(ctx, req))
		var parsed map[string]any
		if err := json.Unmarshal(r.Structured, &parsed); err != nil {
			t.Fatalf("structured output is not JSON: %v", err)
		}
		if _, ok := parsed["answer"].(string); !ok || len(parsed) != 1 {
			t.Fatalf("structured output violates schema: %s", r.Structured)
		}
	})
	conformance.Case(t, "StreamDeltasConcatenateToFinal", func(t *testing.T) {
		h, c := setup(t)
		stream := conformance.Must(c.StreamGenerate(ctx, ask(h, "数到五")))
		defer stream.Close()
		var text strings.Builder
		var final *modelprovider.GenerateResult
		for {
			chunk, err := stream.Recv(ctx)
			if errors.Is(err, io.EOF) {
				break
			}
			conformance.NoError(t, err)
			text.WriteString(chunk.TextDelta)
			if chunk.Final != nil {
				final = chunk.Final
			}
		}
		if final == nil || final.Text != text.String() || final.Usage == nil || final.Usage.GetOutputTokens() <= 0 {
			t.Fatalf("final = %+v, deltas = %q", final, text.String())
		}
	})
	conformance.Case(t, "ScoreAndEmbedKeepOrder", func(t *testing.T) {
		h, c := setup(t)
		scores := conformance.Must(c.Score(ctx, modelprovider.ScoreRequest{
			Model: h.Model(), Query: "退款原因", Candidates: []string{"物流破损", "天气晴朗", "尺码不符"},
		}))
		if len(scores.Scores) != 3 || scores.Usage == nil {
			t.Fatalf("scores = %+v", scores)
		}
		emb := conformance.Must(c.Embed(ctx, modelprovider.EmbedRequest{Model: h.EmbeddingModel(), Texts: []string{"a", "b"}}))
		if len(emb.Vectors) != 2 || len(emb.Vectors[0]) == 0 || len(emb.Vectors[0]) != len(emb.Vectors[1]) || emb.Usage == nil {
			t.Fatalf("embeddings = %d vectors", len(emb.Vectors))
		}
	})
	conformance.Case(t, "RequestErrorsAreClassified", func(t *testing.T) {
		h, c := setup(t)
		_, err := c.Generate(ctx, modelprovider.GenerateRequest{Model: h.Model()})
		conformance.ExpectError(t, err, providererr.Invalid, "")
		_, err = c.Generate(ctx, modelprovider.GenerateRequest{Model: "no-such-model", Messages: ask(h, "x").Messages})
		conformance.ExpectError(t, err, providererr.NotFound, "")
	})
	conformance.Case(t, "RateLimitIsTransient", func(t *testing.T) {
		h, c := setup(t)
		conformance.SkipIfUnsupported(t, h.InjectFault("Generate", providererr.Transient))
		_, err := c.Generate(ctx, ask(h, "x"))
		conformance.ExpectError(t, err, providererr.Transient, "")
		conformance.Must(c.Generate(ctx, ask(h, "x")))
	})
	conformance.Case(t, "CallsUseTheBoundScope", func(t *testing.T) {
		h, c := setup(t)
		conformance.Must(c.Generate(ctx, ask(h, "x")))
		platform := conformance.Must(h.Provider().BindPlatform("conformance"))
		conformance.Must(platform.Generate(ctx, ask(h, "x")))
		calls, err := h.Calls()
		conformance.SkipIfUnsupported(t, err)
		if len(calls) != 2 || calls[0].TenantID != conformance.TenantAID || calls[1].TenantID != "" {
			t.Fatalf("calls = %+v", calls)
		}
	})
}
