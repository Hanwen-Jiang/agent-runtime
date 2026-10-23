// Package consoletelemetry 是控制台 Telemetry（开发实现）。语义见 providers/telemetry。
//
// Flush 时把记录按 JSON 逐行写到 Writer，并保留在内存里供测试检查（Spans、Metrics、Logs）。
// 所有属性在记录时就已脱敏，控制台与内存里都不会出现明文。
package consoletelemetry

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"sync"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

var traceparent = regexp.MustCompile(`^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$`)

// SpanRecord、MetricRecord、LogRecord 是已导出的记录。
type SpanRecord struct {
	Name, TraceID, SpanID, ParentSpanID string
	Attributes                          map[string]any
	Status                              string
	Events                              []string
}

type MetricRecord struct {
	Name, Kind string
	Value      float64
	Attributes map[string]any
}

type LogRecord struct {
	Level, Message string
	Attributes     map[string]any
}

// Options 是构造参数；Writer 为空时写到标准错误。
type Options struct {
	Writer io.Writer
	Faults *testkit.Faults
}

// Provider 是控制台 Telemetry 的工厂。
type Provider struct {
	mu      sync.Mutex
	out     io.Writer
	faults  *testkit.Faults
	pending []any
	spans   []SpanRecord
	metrics []MetricRecord
	logs    []LogRecord
}

// New 构造一个控制台 Telemetry。
func New(opts Options) *Provider {
	out := opts.Writer
	if out == nil {
		out = os.Stderr
	}
	return &Provider{out: out, faults: opts.Faults}
}

func (p *Provider) Bind(t *commonv1.TenantContext) (telemetry.Telemetry, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return newClient(p, s), nil
}

func (p *Provider) BindPlatform(purpose string) (telemetry.Telemetry, error) {
	s, err := tenancy.PlatformScope(purpose)
	if err != nil {
		return nil, err
	}
	return newClient(p, s), nil
}

func (p *Provider) Close() error { return nil }

// Spans 返回已导出的 span（开发专用）。
func (p *Provider) Spans() []SpanRecord {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]SpanRecord(nil), p.spans...)
}

// Metrics 返回已导出的指标（开发专用）。
func (p *Provider) Metrics() []MetricRecord {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]MetricRecord(nil), p.metrics...)
}

// Logs 返回已导出的日志（开发专用）。
func (p *Provider) Logs() []LogRecord {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]LogRecord(nil), p.logs...)
}

func (p *Provider) add(record any) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.pending = append(p.pending, record)
}

// Sanitize 去掉保留属性，脱敏敏感键与 secret.Value（规则见 telemetry.Sanitize）。
func Sanitize(attrs []telemetry.Attribute) map[string]any { return telemetry.Sanitize(attrs) }

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

type client struct {
	p          *Provider
	scope      tenancy.Scope
	scopeAttrs map[string]any
}

func newClient(p *Provider, s tenancy.Scope) *client {
	attrs := map[string]any{}
	if s.IsPlatform() {
		attrs["scope"] = "platform"
	} else {
		for k, v := range map[string]string{"tenant_id": s.TenantID, "user_id": s.UserID, "workspace_id": s.WorkspaceID, "session_id": s.SessionID} {
			if v != "" {
				attrs[k] = v
			}
		}
	}
	return &client{p: p, scope: s, scopeAttrs: attrs}
}

func (c *client) attributes(attrs []telemetry.Attribute) map[string]any {
	out := Sanitize(attrs)
	for k, v := range c.scopeAttrs {
		out[k] = v
	}
	return out
}

func invalidName() error {
	return providererr.New(providererr.Invalid, "invalid_argument", "名称不能为空")
}

type spanKey struct{}

type span struct {
	c      *client
	record SpanRecord
	mu     sync.Mutex
	ended  bool
}

func (s *span) TraceID() string { return s.record.TraceID }
func (s *span) SpanID() string  { return s.record.SpanID }

func (s *span) SetAttributes(attrs ...telemetry.Attribute) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for k, v := range Sanitize(attrs) {
		s.record.Attributes[k] = v
	}
}

func (s *span) AddEvent(name string, attrs ...telemetry.Attribute) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if clean := Sanitize(attrs); len(clean) > 0 {
		raw, _ := json.Marshal(clean)
		name += " " + string(raw)
	}
	s.record.Events = append(s.record.Events, name)
}

func (s *span) RecordError(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.record.Status = "error"
	s.record.Attributes["error.type"] = fmt.Sprintf("%T", err)
	s.record.Events = append(s.record.Events, "exception")
}

func (s *span) End() {
	s.mu.Lock()
	if s.ended {
		s.mu.Unlock()
		return
	}
	s.ended = true
	for k, v := range s.c.scopeAttrs { // SetAttributes 不能覆盖租户属性
		s.record.Attributes[k] = v
	}
	record := s.record
	s.mu.Unlock()
	s.c.p.add(record)
}

func (c *client) StartSpan(ctx context.Context, name string, attrs ...telemetry.Attribute) (context.Context, telemetry.Span, error) {
	if name == "" {
		return ctx, nil, invalidName()
	}
	traceID, parentID := randomHex(16), ""
	if parent, ok := ctx.Value(spanKey{}).(*span); ok {
		traceID, parentID = parent.record.TraceID, parent.record.SpanID
	} else if m := traceparent.FindStringSubmatch(c.scope.Traceparent); m != nil {
		traceID, parentID = m[1], m[2]
	}
	s := &span{c: c, record: SpanRecord{
		Name: name, TraceID: traceID, SpanID: randomHex(8), ParentSpanID: parentID,
		Attributes: c.attributes(attrs), Status: "ok",
	}}
	return context.WithValue(ctx, spanKey{}, s), s, nil
}

func (c *client) Count(_ context.Context, name string, value int64, attrs ...telemetry.Attribute) error {
	if name == "" {
		return invalidName()
	}
	c.p.add(MetricRecord{Name: name, Kind: "counter", Value: float64(value), Attributes: c.attributes(attrs)})
	return nil
}

func (c *client) Observe(_ context.Context, name string, value float64, attrs ...telemetry.Attribute) error {
	if name == "" {
		return invalidName()
	}
	c.p.add(MetricRecord{Name: name, Kind: "histogram", Value: value, Attributes: c.attributes(attrs)})
	return nil
}

func (c *client) Log(_ context.Context, level telemetry.Level, message string, attrs ...telemetry.Attribute) error {
	c.p.add(LogRecord{Level: string(level), Message: message, Attributes: c.attributes(attrs)})
	return nil
}

func (c *client) Flush(_ context.Context) error {
	if err := c.p.faults.Check("export"); err != nil {
		return err
	}
	p := c.p
	p.mu.Lock()
	defer p.mu.Unlock()
	enc := json.NewEncoder(p.out)
	for _, r := range p.pending {
		switch x := r.(type) {
		case SpanRecord:
			p.spans = append(p.spans, x)
			_ = enc.Encode(map[string]any{"kind": "span", "record": x})
		case MetricRecord:
			p.metrics = append(p.metrics, x)
			_ = enc.Encode(map[string]any{"kind": "metric", "record": x})
		case LogRecord:
			p.logs = append(p.logs, x)
			_ = enc.Encode(map[string]any{"kind": "log", "record": x})
		}
	}
	p.pending = nil
	return nil
}
