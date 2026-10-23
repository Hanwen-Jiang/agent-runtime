// Package oteltelemetry 是 OpenTelemetry 实现的 Telemetry（设计书 4.13；T-15，细化见 docs/plan/deploy-telemetry.md）。
// 语义见 providers/telemetry，与 Python 的 agentruntime_core.adapters.otel 一致：
//   - span、指标、日志都经 OTLP/HTTP（protobuf）发到同一个 Collector：<Endpoint>/v1/traces、/v1/metrics、/v1/logs。
//   - 资源属性只有服务名、版本与环境；租户属性由绑定加在每条记录上。属性规则见 telemetry.Sanitize。
//   - 打点只进 SDK 的缓冲，后台按批导出；Flush 强制导出一次，等待不超过导出超时（另加一点余量）。期间任何一次导出失败
//     （含后台的），Flush 返回 Transient；StartSpan、Count、Observe、Log 只在参数不合法时返回 Invalid。
//   - 指标按 delta 时间性导出：Count 是 Int64 counter，Observe 是 Float64 histogram。
//   - RecordError 只记错误类型（状态 error、属性 error.type、事件 exception），不记消息：消息里可能有凭据。
//   - 不设置 OpenTelemetry 的全局 Provider；嵌套关系用 ctx 里的 span。
package oteltelemetry

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	otellog "go.opentelemetry.io/otel/log"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/consoletelemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// 环境变量（OpenTelemetry 的标准名）。
const (
	EnvEndpoint       = "OTEL_EXPORTER_OTLP_ENDPOINT"
	EnvServiceVersion = "OTEL_SERVICE_VERSION"
	EnvEnvironment    = "OTEL_DEPLOYMENT_ENVIRONMENT"
)

// DefaultExportTimeout 是 Options.ExportTimeout 为 0 时的导出超时。
const DefaultExportTimeout = 2 * time.Second

// 后台导出的间隔：调用方可以随时 Flush，后台只兜底。
const (
	scheduleDelay  = 5 * time.Second
	metricInterval = 60 * time.Second
)

// Options 是构造参数；Endpoint 与 ServiceName 必填。
type Options struct {
	// Endpoint 是 Collector 的 OTLP/HTTP 基地址，形如 http://otel-collector:4318。
	Endpoint       string
	ServiceName    string
	ServiceVersion string
	Environment    string
	ExportTimeout  time.Duration
}

// Provider 是 OpenTelemetry 实现的工厂。
type Provider struct {
	opts     Options
	failed   atomic.Bool
	tp       *sdktrace.TracerProvider
	mp       *sdkmetric.MeterProvider
	lp       *sdklog.LoggerProvider
	tracer   trace.Tracer
	meter    metric.Meter
	logger   otellog.Logger
	mu       sync.Mutex
	counters map[string]metric.Int64Counter
	hists    map[string]metric.Float64Histogram
	closed   atomic.Bool
}

var _ telemetry.Provider = (*Provider)(nil)

// New 构造 Provider；不连接 Collector（第一次导出时才连）。
func New(opts Options) (*Provider, error) {
	opts.Endpoint = strings.TrimRight(strings.TrimSpace(opts.Endpoint), "/")
	if opts.Endpoint == "" {
		return nil, providererr.New(providererr.Invalid, "invalid_argument", "Collector 的地址不能为空")
	}
	if opts.ServiceName == "" {
		return nil, providererr.New(providererr.Invalid, "invalid_argument", "服务名不能为空")
	}
	if opts.ExportTimeout <= 0 {
		opts.ExportTimeout = DefaultExportTimeout
	}
	attrs := []attribute.KeyValue{attribute.String("service.name", opts.ServiceName)}
	if opts.ServiceVersion != "" {
		attrs = append(attrs, attribute.String("service.version", opts.ServiceVersion))
	}
	if opts.Environment != "" {
		attrs = append(attrs, attribute.String("deployment.environment", opts.Environment))
	}
	res, err := resource.Merge(resource.Default(), resource.NewSchemaless(attrs...))
	if err != nil {
		return nil, providererr.Wrap(providererr.Invalid, "invalid_argument", "资源属性不合法", err)
	}
	ctx := context.Background()
	// 可重试的失败（503、连接错误）按退避重试，总时长不超过导出超时。
	enabled, initial, maxInterval, maxElapsed := true, 100*time.Millisecond, opts.ExportTimeout/2, opts.ExportTimeout
	p := &Provider{opts: opts, counters: map[string]metric.Int64Counter{}, hists: map[string]metric.Float64Histogram{}}

	spanExp, err := otlptracehttp.New(ctx,
		otlptracehttp.WithEndpointURL(opts.Endpoint+"/v1/traces"),
		otlptracehttp.WithTimeout(opts.ExportTimeout),
		otlptracehttp.WithRetry(otlptracehttp.RetryConfig{Enabled: enabled, InitialInterval: initial, MaxInterval: maxInterval, MaxElapsedTime: maxElapsed}))
	if err != nil {
		return nil, providererr.Wrap(providererr.Invalid, "invalid_argument", "span 导出器配置不合法", err)
	}
	p.tp = sdktrace.NewTracerProvider(
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
		sdktrace.WithBatcher(&spanExporter{SpanExporter: spanExp, p: p}, sdktrace.WithBatchTimeout(scheduleDelay), sdktrace.WithExportTimeout(opts.ExportTimeout)),
	)
	p.tracer = p.tp.Tracer("agentruntime")

	metricExp, err := otlpmetrichttp.New(ctx,
		otlpmetrichttp.WithEndpointURL(opts.Endpoint+"/v1/metrics"),
		otlpmetrichttp.WithTimeout(opts.ExportTimeout),
		otlpmetrichttp.WithRetry(otlpmetrichttp.RetryConfig{Enabled: enabled, InitialInterval: initial, MaxInterval: maxInterval, MaxElapsedTime: maxElapsed}),
		otlpmetrichttp.WithTemporalitySelector(func(sdkmetric.InstrumentKind) metricdata.Temporality { return metricdata.DeltaTemporality }))
	if err != nil {
		return nil, providererr.Wrap(providererr.Invalid, "invalid_argument", "指标导出器配置不合法", err)
	}
	p.mp = sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(&metricExporter{Exporter: metricExp, p: p}, sdkmetric.WithInterval(metricInterval), sdkmetric.WithTimeout(opts.ExportTimeout))),
	)
	p.meter = p.mp.Meter("agentruntime")

	logExp, err := otlploghttp.New(ctx,
		otlploghttp.WithEndpointURL(opts.Endpoint+"/v1/logs"),
		otlploghttp.WithTimeout(opts.ExportTimeout),
		otlploghttp.WithRetry(otlploghttp.RetryConfig{Enabled: enabled, InitialInterval: initial, MaxInterval: maxInterval, MaxElapsedTime: maxElapsed}))
	if err != nil {
		return nil, providererr.Wrap(providererr.Invalid, "invalid_argument", "日志导出器配置不合法", err)
	}
	p.lp = sdklog.NewLoggerProvider(
		sdklog.WithResource(res),
		sdklog.WithProcessor(sdklog.NewBatchProcessor(&logExporter{Exporter: logExp, p: p}, sdklog.WithExportInterval(scheduleDelay), sdklog.WithExportTimeout(opts.ExportTimeout))),
	)
	p.logger = p.lp.Logger("agentruntime")
	return p, nil
}

// FromEnv 按 OTEL_EXPORTER_OTLP_ENDPOINT 选实现：设了用 OpenTelemetry，没设（或只有空白）用控制台实现，写到 fallback。
func FromEnv(service string, getenv func(string) string, fallback io.Writer) (telemetry.Provider, error) {
	endpoint := strings.TrimSpace(getenv(EnvEndpoint))
	if endpoint == "" {
		return consoletelemetry.New(consoletelemetry.Options{Writer: fallback}), nil
	}
	return New(Options{
		Endpoint: endpoint, ServiceName: service,
		ServiceVersion: strings.TrimSpace(getenv(EnvServiceVersion)), Environment: strings.TrimSpace(getenv(EnvEnvironment)),
	})
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

// Close 关闭三个 SDK Provider（剩下的记录尽量导出，等待不超过导出超时）。可以重复调用。
func (p *Provider) Close() error {
	if p.closed.Swap(true) {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), p.opts.ExportTimeout+500*time.Millisecond)
	defer cancel()
	var wg sync.WaitGroup
	for _, shutdown := range []func(context.Context) error{p.tp.Shutdown, p.mp.Shutdown, p.lp.Shutdown} {
		wg.Add(1)
		go func() { defer wg.Done(); _ = shutdown(ctx) }()
	}
	wg.Wait()
	return nil
}

func (p *Provider) flush(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, p.opts.ExportTimeout+500*time.Millisecond)
	defer cancel()
	errs := make([]error, 3)
	var wg sync.WaitGroup
	for i, force := range []func(context.Context) error{p.tp.ForceFlush, p.mp.ForceFlush, p.lp.ForceFlush} {
		wg.Add(1)
		go func() { defer wg.Done(); errs[i] = force(ctx) }()
	}
	wg.Wait()
	failed := p.failed.Swap(false)
	if err := errors.Join(errs...); err != nil || failed {
		return providererr.Wrap(providererr.Transient, "export_failed", "导出到 Collector 失败", err)
	}
	return nil
}

// ---- 记下导出失败的导出器包装（后台导出与 Flush 都经过这里）----

type spanExporter struct {
	sdktrace.SpanExporter
	p *Provider
}

func (e *spanExporter) ExportSpans(ctx context.Context, spans []sdktrace.ReadOnlySpan) error {
	err := e.SpanExporter.ExportSpans(ctx, spans)
	if err != nil {
		e.p.failed.Store(true)
	}
	return err
}

type metricExporter struct {
	sdkmetric.Exporter
	p *Provider
}

func (e *metricExporter) Export(ctx context.Context, rm *metricdata.ResourceMetrics) error {
	err := e.Exporter.Export(ctx, rm)
	if err != nil {
		e.p.failed.Store(true)
	}
	return err
}

type logExporter struct {
	sdklog.Exporter
	p *Provider
}

func (e *logExporter) Export(ctx context.Context, records []sdklog.Record) error {
	err := e.Exporter.Export(ctx, records)
	if err != nil {
		e.p.failed.Store(true)
	}
	return err
}

// ---- 绑定的客户端 ----

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

func invalidName(name string) error {
	if name == "" {
		return providererr.New(providererr.Invalid, "invalid_argument", "名称不能为空")
	}
	return providererr.New(providererr.Invalid, "invalid_argument", "名称不合法："+name)
}

// kv 把脱敏后的属性转成 OpenTelemetry 的属性；租户属性最后加，覆盖不了。
func (c *client) kv(attrs []telemetry.Attribute) []attribute.KeyValue {
	clean := telemetry.Sanitize(attrs)
	for k, v := range c.scopeAttrs {
		clean[k] = v
	}
	return toKV(clean)
}

func toKV(m map[string]any) []attribute.KeyValue {
	out := make([]attribute.KeyValue, 0, len(m))
	for k, v := range m {
		out = append(out, toAttribute(k, v))
	}
	return out
}

func toAttribute(k string, v any) attribute.KeyValue {
	switch x := v.(type) {
	case string:
		return attribute.String(k, x)
	case bool:
		return attribute.Bool(k, x)
	case int:
		return attribute.Int(k, x)
	case int32:
		return attribute.Int64(k, int64(x))
	case int64:
		return attribute.Int64(k, x)
	case float32:
		return attribute.Float64(k, float64(x))
	case float64:
		return attribute.Float64(k, x)
	case []string:
		return attribute.StringSlice(k, x)
	case []bool:
		return attribute.BoolSlice(k, x)
	case []int64:
		return attribute.Int64Slice(k, x)
	case []int:
		return attribute.IntSlice(k, x)
	case []float64:
		return attribute.Float64Slice(k, x)
	case []any:
		s := make([]string, len(x))
		for i, e := range x {
			s[i] = fmt.Sprint(e)
		}
		return attribute.StringSlice(k, s)
	}
	return attribute.String(k, fmt.Sprint(v))
}

type span struct {
	c    *client
	span trace.Span
}

func (s *span) TraceID() string { return s.span.SpanContext().TraceID().String() }
func (s *span) SpanID() string  { return s.span.SpanContext().SpanID().String() }

func (s *span) SetAttributes(attrs ...telemetry.Attribute) {
	s.span.SetAttributes(toKV(telemetry.Sanitize(attrs))...)
}

func (s *span) AddEvent(name string, attrs ...telemetry.Attribute) {
	s.span.AddEvent(name, trace.WithAttributes(toKV(telemetry.Sanitize(attrs))...))
}

func (s *span) RecordError(err error) {
	typ := fmt.Sprintf("%T", err)
	s.span.SetStatus(codes.Error, "")
	s.span.SetAttributes(attribute.String("error.type", typ))
	s.span.AddEvent("exception", trace.WithAttributes(attribute.String("exception.type", typ)))
}

func (s *span) End() {
	s.span.SetAttributes(toKV(s.c.scopeAttrs)...) // SetAttributes 不能覆盖租户属性
	s.span.End()
}

func (c *client) StartSpan(ctx context.Context, name string, attrs ...telemetry.Attribute) (context.Context, telemetry.Span, error) {
	if name == "" {
		return ctx, nil, invalidName(name)
	}
	parent := ctx
	if !trace.SpanContextFromContext(ctx).IsValid() {
		// 不在本进程的 span 里：接上租户上下文的 traceparent（没有时是新的 trace）。
		if c.scope.Traceparent != "" {
			parent = propagation.TraceContext{}.Extract(ctx, propagation.MapCarrier{"traceparent": c.scope.Traceparent})
		}
	}
	out, s := c.p.tracer.Start(parent, name, trace.WithAttributes(c.kv(attrs)...))
	return out, &span{c: c, span: s}, nil
}

func (c *client) Count(ctx context.Context, name string, value int64, attrs ...telemetry.Attribute) error {
	if name == "" {
		return invalidName(name)
	}
	p := c.p
	p.mu.Lock()
	counter, ok := p.counters[name]
	if !ok {
		var err error
		if counter, err = p.meter.Int64Counter(name); err != nil {
			p.mu.Unlock()
			return invalidName(name)
		}
		p.counters[name] = counter
	}
	p.mu.Unlock()
	counter.Add(ctx, value, metric.WithAttributes(c.kv(attrs)...))
	return nil
}

func (c *client) Observe(ctx context.Context, name string, value float64, attrs ...telemetry.Attribute) error {
	if name == "" {
		return invalidName(name)
	}
	p := c.p
	p.mu.Lock()
	hist, ok := p.hists[name]
	if !ok {
		var err error
		if hist, err = p.meter.Float64Histogram(name); err != nil {
			p.mu.Unlock()
			return invalidName(name)
		}
		p.hists[name] = hist
	}
	p.mu.Unlock()
	hist.Record(ctx, value, metric.WithAttributes(c.kv(attrs)...))
	return nil
}

var severities = map[telemetry.Level]otellog.Severity{
	telemetry.LevelDebug:   otellog.SeverityDebug,
	telemetry.LevelInfo:    otellog.SeverityInfo,
	telemetry.LevelWarning: otellog.SeverityWarn,
	telemetry.LevelError:   otellog.SeverityError,
}

func (c *client) Log(ctx context.Context, level telemetry.Level, message string, attrs ...telemetry.Attribute) error {
	var r otellog.Record
	r.SetTimestamp(time.Now())
	r.SetBody(otellog.StringValue(message))
	r.SetSeverity(severities[level])
	r.SetSeverityText(string(level))
	for _, a := range c.kv(attrs) {
		r.AddAttributes(otellog.KeyValueFromAttribute(a))
	}
	c.p.logger.Emit(ctx, r)
	return nil
}

func (c *client) Flush(ctx context.Context) error { return c.p.flush(ctx) }
