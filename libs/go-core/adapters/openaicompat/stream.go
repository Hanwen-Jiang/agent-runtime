package openaicompat

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
)

func (c *client) StreamGenerate(ctx context.Context, req modelprovider.GenerateRequest) (modelprovider.Stream, error) {
	body, err := chatRequest(req, true)
	if err != nil {
		return nil, err
	}
	r, err := c.post(ctx, chatPath, req.Model, body)
	if err != nil {
		return nil, err
	}
	return &stream{resp: r, lines: bufio.NewReaderSize(r.body, 64<<10), schema: req.OutputSchema}, nil
}

// partialCall 是流式里还没拼完的工具调用。
type partialCall struct {
	id, name string
	args     strings.Builder
}

// stream 读 SSE：data 行逐个解析，注释行与空行跳过，data: [DONE] 结束；没有 [DONE] 就断开为 Transient。
// 工具调用的参数分片拼完整后才交出：下一个调用开始、finish_reason 到达或 [DONE] 时交出之前的调用。
type stream struct {
	resp   *response
	lines  *bufio.Reader
	schema json.RawMessage

	pending  []modelprovider.Chunk
	text     strings.Builder
	thinking strings.Builder
	calls    []*partialCall
	emitted  int
	finish   string
	usage    *wireUsage
	// final 为 true 表示 Final 已排进 pending；之后读完 pending 就一直返回 io.EOF。
	final  bool
	err    error
	closed bool
}

func (s *stream) Format(f fmt.State, _ rune) { _, _ = io.WriteString(f, "openaicompat.stream") }

type wireDelta struct {
	Content          *string        `json:"content"`
	ReasoningContent *string        `json:"reasoning_content"`
	Reasoning        *string        `json:"reasoning"`
	ToolCalls        []wireToolCall `json:"tool_calls"`
}

type wireStreamChunk struct {
	Choices []struct {
		Delta        wireDelta `json:"delta"`
		FinishReason *string   `json:"finish_reason"`
	} `json:"choices"`
	Usage *wireUsage `json:"usage"`
}

func (s *stream) Recv(ctx context.Context) (modelprovider.Chunk, error) {
	for {
		if len(s.pending) > 0 {
			chunk := s.pending[0]
			s.pending = s.pending[1:]
			if chunk.Final != nil {
				s.Close()
			}
			return chunk, nil
		}
		if s.err != nil {
			return modelprovider.Chunk{}, s.err
		}
		if s.final {
			return modelprovider.Chunk{}, io.EOF
		}
		if err := ctx.Err(); err != nil {
			s.fail(transportError(ctx, err))
			continue
		}
		s.step()
	}
}

// fail 记下错误并释放连接；之后 Recv 一直返回这个错误。
func (s *stream) fail(err error) {
	s.err = err
	s.Close()
}

// step 读一行并处理。
func (s *stream) step() {
	line, err := s.lines.ReadString('\n')
	if err != nil && (line == "" || !errors.Is(err, io.EOF)) {
		if errors.Is(err, io.EOF) {
			s.fail(providererr.New(providererr.Transient, "stream_truncated", "上游在流式回复结束前断开"))
			return
		}
		s.fail(transportError(s.resp.ctx, err))
		return
	}
	line = strings.TrimRight(line, "\r\n")
	if !strings.HasPrefix(line, "data:") {
		return // 空行、注释行（以 : 开头）与 event: 之类的字段都跳过
	}
	data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
	if data == "[DONE]" {
		s.finishStream()
		return
	}
	var chunk wireStreamChunk
	if json.Unmarshal([]byte(data), &chunk) != nil {
		s.fail(providererr.New(providererr.Transient, "bad_response", "上游的流式分片不是合法 JSON"))
		return
	}
	if chunk.Usage != nil {
		s.usage = chunk.Usage
	}
	for _, choice := range chunk.Choices {
		d := choice.Delta
		thinking := d.ReasoningContent
		if thinking == nil {
			thinking = d.Reasoning
		}
		if thinking != nil && *thinking != "" {
			s.thinking.WriteString(*thinking)
			s.pending = append(s.pending, modelprovider.Chunk{ThinkingDelta: *thinking})
		}
		if d.Content != nil && *d.Content != "" {
			s.text.WriteString(*d.Content)
			s.pending = append(s.pending, modelprovider.Chunk{TextDelta: *d.Content})
		}
		for _, tc := range d.ToolCalls {
			s.addToolCall(tc)
		}
		if choice.FinishReason != nil && *choice.FinishReason != "" {
			s.finish = *choice.FinishReason
			s.flushCalls(len(s.calls))
		}
	}
}

// addToolCall 合并一个工具调用分片；新的调用开始时交出之前拼完的调用。
func (s *stream) addToolCall(tc wireToolCall) {
	index := len(s.calls) - 1
	if tc.Index != nil {
		index = *tc.Index
	} else if tc.ID != "" {
		index = len(s.calls)
	}
	if index < 0 {
		index = 0
	}
	if index >= len(s.calls) {
		s.flushCalls(len(s.calls))
		for len(s.calls) <= index {
			s.calls = append(s.calls, &partialCall{})
		}
	}
	call := s.calls[index]
	if tc.ID != "" {
		call.id = tc.ID
	}
	if tc.Function.Name != "" {
		call.name = tc.Function.Name
	}
	call.args.WriteString(tc.Function.Arguments)
}

// flushCalls 把前 n 个还没交出的工具调用排进 pending。
func (s *stream) flushCalls(n int) {
	for ; s.emitted < n; s.emitted++ {
		call := s.calls[s.emitted]
		args, err := arguments(call.args.String())
		if err != nil {
			s.fail(err)
			return
		}
		s.pending = append(s.pending, modelprovider.Chunk{ToolCall: &modelprovider.ToolCall{ID: call.id, Name: call.name, Arguments: args}})
	}
}

// finishStream 在 [DONE] 时交出剩下的工具调用与 Final。
func (s *stream) finishStream() {
	s.flushCalls(len(s.calls))
	if s.err != nil {
		return
	}
	final := &modelprovider.GenerateResult{
		Text:         s.text.String(),
		Thinking:     s.thinking.String(),
		FinishReason: finishReason(s.finish, len(s.calls) > 0),
		Usage:        s.usage.proto(),
		ToolCalls:    s.allCalls(),
	}
	if len(s.schema) > 0 && len(final.ToolCalls) == 0 {
		structuredOut, err := structured(final.Text)
		if err != nil {
			s.fail(err)
			return
		}
		final.Structured = structuredOut
	}
	s.pending = append(s.pending, modelprovider.Chunk{Final: final})
	s.final = true
}

// allCalls 返回全部工具调用（已经校验过参数）。
func (s *stream) allCalls() []modelprovider.ToolCall {
	var out []modelprovider.ToolCall
	for _, call := range s.calls {
		args, _ := arguments(call.args.String())
		out = append(out, modelprovider.ToolCall{ID: call.id, Name: call.name, Arguments: args})
	}
	return out
}

// Close 释放连接；可以重复调用。
func (s *stream) Close() error {
	if s.closed {
		return nil
	}
	s.closed = true
	_ = s.resp.body.Close() // 关闭连接时的错误对调用方没有意义
	s.resp.done()
	return nil
}
