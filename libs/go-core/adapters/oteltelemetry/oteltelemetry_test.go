package oteltelemetry_test

// T-15：OpenTelemetry 实现的 Telemetry（设计书 4.13；细化见 docs/plan/deploy-telemetry.md）。
// 记录经真的 OTLP/HTTP 导出器发到假 Collector，解码之后跑与控制台实现相同的一致性测试（telemetrytest.Run）；
// 另外检查资源属性、导出失败与 flush 的超时、按环境变量选实现。

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	collogs "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	colmetrics "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	coltrace "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/oteltelemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/telemetrytest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/consoletelemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
)

const exportTimeout = time.Second

// fakeCollector 收 OTLP/HTTP（protobuf）的 /v1/traces、/v1/metrics、/v1/logs，解码成一致性测试的 Exported。
type fakeCollector struct {
	srv       *httptest.Server
	mu        sync.Mutex
	exported  telemetrytest.Exported
	resources []map[string]any
	paths     map[string]bool
	raw       bytes.Buffer
	failing   bool
	delay     time.Duration
}

func newCollector(t *testing.T) *fakeCollector {
	c := &fakeCollector{paths: map[string]bool{}}
	c.srv = httptest.NewServer(http.HandlerFunc(c.handle))
	t.Cleanup(c.srv.Close)
	return c
}

func (c *fakeCollector) handle(w http.ResponseWriter, r *http.Request) {
	var body io.Reader = r.Body
	if r.Header.Get("Content-Encoding") == "gzip" {
		zr, err := gzip.NewReader(r.Body)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		body = zr
	}
	data, _ := io.ReadAll(body)
	c.mu.Lock()
	delay, failing := c.delay, c.failing
	c.paths[r.URL.Path] = true
	c.mu.Unlock()
	if delay > 0 {
		time.Sleep(delay)
	}
	if failing {
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	if r.Header.Get("Content-Type") != "application/x-protobuf" {
		w.WriteHeader(http.StatusUnsupportedMediaType)
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.raw.Write(data)
	if err := c.decode(r.URL.Path, data); err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	w.Header().Set("Content-Type", "application/x-protobuf")
	w.WriteHeader(http.StatusOK)
}

func anyValue(v *commonpb.AnyValue) any {
	switch x := v.GetValue().(type) {
	case *commonpb.AnyValue_StringValue:
		return x.StringValue
	case *commonpb.AnyValue_BoolValue:
		return x.BoolValue
	case *commonpb.AnyValue_IntValue:
		return x.IntValue
	case *commonpb.AnyValue_DoubleValue:
		return x.DoubleValue
	case *commonpb.AnyValue_ArrayValue:
		out := make([]any, 0, len(x.ArrayValue.GetValues()))
		for _, e := range x.ArrayValue.GetValues() {
			out = append(out, anyValue(e))
		}
		return out
	case *commonpb.AnyValue_BytesValue:
		return hex.EncodeToString(x.BytesValue)
	}
	return nil
}

func attrs(kvs []*commonpb.KeyValue) map[string]any {
	out := map[string]any{}
	for _, kv := range kvs {
		out[kv.GetKey()] = anyValue(kv.GetValue())
	}
	return out
}

func (c *fakeCollector) decode(path string, data []byte) error {
	switch path {
	case "/v1/traces":
		var req coltrace.ExportTraceServiceRequest
		if err := proto.Unmarshal(data, &req); err != nil {
			return err
		}
		for _, rs := range req.GetResourceSpans() {
			c.resources = append(c.resources, attrs(rs.GetResource().GetAttributes()))
			for _, ss := range rs.GetScopeSpans() {
				for _, s := range ss.GetSpans() {
					status := "ok"
					if s.GetStatus().GetCode() == tracepb.Status_STATUS_CODE_ERROR {
						status = "error"
					}
					var events []string
					for _, e := range s.GetEvents() {
						a := attrs(e.GetAttributes())
						if len(a) == 0 {
							events = append(events, e.GetName())
							continue
						}
						b, _ := json.Marshal(a)
						events = append(events, e.GetName()+" "+string(b))
					}
					c.exported.Spans = append(c.exported.Spans, telemetrytest.ExportedSpan{
						Name: s.GetName(), TraceID: hex.EncodeToString(s.GetTraceId()), SpanID: hex.EncodeToString(s.GetSpanId()),
						ParentSpanID: hex.EncodeToString(s.GetParentSpanId()), Attributes: attrs(s.GetAttributes()), Status: status, Events: events,
					})
				}
			}
		}
	case "/v1/metrics":
		var req colmetrics.ExportMetricsServiceRequest
		if err := proto.Unmarshal(data, &req); err != nil {
			return err
		}
		for _, rm := range req.GetResourceMetrics() {
			c.resources = append(c.resources, attrs(rm.GetResource().GetAttributes()))
			for _, sm := range rm.GetScopeMetrics() {
				for _, m := range sm.GetMetrics() {
					if sum := m.GetSum(); sum != nil {
						for _, p := range sum.GetDataPoints() {
							v := float64(p.GetAsInt())
							if p.GetAsInt() == 0 {
								v = p.GetAsDouble()
							}
							c.exported.Metrics = append(c.exported.Metrics, telemetrytest.ExportedMetric{Name: m.GetName(), Kind: "counter", Value: v, Attributes: attrs(p.GetAttributes())})
						}
					}
					if h := m.GetHistogram(); h != nil {
						for _, p := range h.GetDataPoints() {
							c.exported.Metrics = append(c.exported.Metrics, telemetrytest.ExportedMetric{Name: m.GetName(), Kind: "histogram", Value: p.GetSum(), Attributes: attrs(p.GetAttributes())})
						}
					}
				}
			}
		}
	case "/v1/logs":
		var req collogs.ExportLogsServiceRequest
		if err := proto.Unmarshal(data, &req); err != nil {
			return err
		}
		for _, rl := range req.GetResourceLogs() {
			c.resources = append(c.resources, attrs(rl.GetResource().GetAttributes()))
			for _, sl := range rl.GetScopeLogs() {
				for _, r := range sl.GetLogRecords() {
					c.exported.Logs = append(c.exported.Logs, telemetrytest.ExportedLog{
						Level: strings.ToLower(r.GetSeverityText()), Message: fmt.Sprint(anyValue(r.GetBody())), Attributes: attrs(r.GetAttributes()),
					})
				}
			}
		}
	default:
		return fmt.Errorf("unexpected path %s", path)
	}
	return nil
}

func (c *fakeCollector) snapshot() telemetrytest.Exported {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.exported
	// 线上传输的原始字节也参与明文检查。
	e.Logs = append(append([]telemetrytest.ExportedLog(nil), e.Logs...), telemetrytest.ExportedLog{Level: "wire", Message: c.raw.String()})
	return e
}

func (c *fakeCollector) seen(path string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.paths[path]
}

func transient(err error) bool { return err != nil && providererr.CategoryOf(err) == providererr.Transient }

func (c *fakeCollector) set(failing bool, delay time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.failing, c.delay = failing, delay
}

type harness struct {
	provider  *oteltelemetry.Provider
	collector *fakeCollector
}

func (h *harness) Provider() telemetry.Provider { return h.provider }

func (h *harness) InjectFault(op string, c providererr.Category) error {
	if op != "export" || c != providererr.Transient {
		return fmt.Errorf("unsupported fault %s/%s", op, c)
	}
	h.collector.set(true, 0)
	return nil
}

func (h *harness) Exported() telemetrytest.Exported { return h.collector.snapshot() }

func newHarness(t *testing.T) *harness {
	c := newCollector(t)
	p, err := oteltelemetry.New(oteltelemetry.Options{
		Endpoint: c.srv.URL, ServiceName: "conformance", ServiceVersion: "0.0.0", Environment: "test", ExportTimeout: exportTimeout,
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = p.Close() })
	return &harness{provider: p, collector: c}
}

func TestConformance(t *testing.T) {
	telemetrytest.Run(t, func(t *testing.T) telemetrytest.Harness { return newHarness(t) })
}

func TestRecordsTravelAsOTLPHTTPProtobuf(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	tel := conformance.Must(h.provider.Bind(conformance.TenantA()))
	ctx, span, err := tel.StartSpan(ctx, "runtime.round")
	conformance.NoError(t, err)
	conformance.NoError(t, tel.Count(ctx, "rounds", 1))
	conformance.NoError(t, tel.Log(ctx, telemetry.LevelInfo, "round started"))
	span.End()
	conformance.NoError(t, tel.Flush(ctx))
	for _, p := range []string{"/v1/traces", "/v1/metrics", "/v1/logs"} {
		if !h.collector.seen(p) {
			t.Errorf("没有收到 %s", p)
		}
	}
}

func TestResourceNamesTheServiceAndNeverATenant(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	tel := conformance.Must(h.provider.Bind(conformance.TenantA()))
	_, span, err := tel.StartSpan(ctx, "node")
	conformance.NoError(t, err)
	span.End()
	conformance.NoError(t, tel.Count(ctx, "calls", 1))
	conformance.NoError(t, tel.Flush(ctx))
	if len(h.collector.resources) == 0 {
		t.Fatal("每批记录都带资源属性")
	}
	for _, r := range h.collector.resources {
		if r["service.name"] != "conformance" || r["service.version"] != "0.0.0" || r["deployment.environment"] != "test" {
			t.Errorf("资源属性不对：%v", r)
		}
		for _, k := range []string{"tenant_id", "user_id", "session_id"} {
			if _, ok := r[k]; ok {
				t.Errorf("资源属性里不能有 %s（一个进程服务多个租户）", k)
			}
		}
	}
}

func TestUnreachableCollectorOnlyShowsUpInFlush(t *testing.T) {
	h := newHarness(t)
	h.collector.set(true, 0)
	ctx := context.Background()
	tel := conformance.Must(h.provider.Bind(conformance.TenantA()))
	ctx, span, err := tel.StartSpan(ctx, "node")
	conformance.NoError(t, err)
	conformance.NoError(t, tel.Count(ctx, "calls", 1))
	conformance.NoError(t, tel.Observe(ctx, "latency_ms", 3))
	conformance.NoError(t, tel.Log(ctx, telemetry.LevelInfo, "still fine"))
	span.End()
	err = tel.Flush(ctx)
	if !transient(err) {
		t.Fatalf("导出失败只能在 flush 体现且为 transient，得到 %v", err)
	}
}

func TestFlushGivesUpWithinTheExportTimeout(t *testing.T) {
	h := newHarness(t)
	h.collector.set(false, 10*time.Second)
	ctx := context.Background()
	tel := conformance.Must(h.provider.Bind(conformance.TenantA()))
	_, span, err := tel.StartSpan(ctx, "node")
	conformance.NoError(t, err)
	span.End()
	started := time.Now()
	err = tel.Flush(ctx)
	if !transient(err) {
		t.Fatalf("Collector 卡住时 flush 应以 transient 返回，得到 %v", err)
	}
	if elapsed := time.Since(started); elapsed > 3*exportTimeout+time.Second {
		t.Fatalf("flush 用了 %v，超过导出超时的上限", elapsed)
	}
}

func TestStartSpanDoesNotWaitForTheCollector(t *testing.T) {
	h := newHarness(t)
	h.collector.set(false, 5*time.Second)
	ctx := context.Background()
	tel := conformance.Must(h.provider.Bind(conformance.TenantA()))
	started := time.Now()
	for i := 0; i < 20; i++ {
		c, span, err := tel.StartSpan(ctx, "node")
		conformance.NoError(t, err)
		conformance.NoError(t, tel.Count(c, "calls", 1))
		span.End()
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("打点只进缓冲，不等导出；用了 %v", elapsed)
	}
}

func TestFromEnvSelectsTheImplementation(t *testing.T) {
	env := func(m map[string]string) func(string) string { return func(k string) string { return m[k] } }
	p, err := oteltelemetry.FromEnv("model-gateway", env(map[string]string{"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}), io.Discard)
	conformance.NoError(t, err)
	defer p.Close()
	if _, ok := p.(*oteltelemetry.Provider); !ok {
		t.Fatalf("设了 OTEL_EXPORTER_OTLP_ENDPOINT 应得到 OpenTelemetry 实现，得到 %T", p)
	}
	for _, m := range []map[string]string{{}, {"OTEL_EXPORTER_OTLP_ENDPOINT": "  "}} {
		p, err := oteltelemetry.FromEnv("model-gateway", env(m), io.Discard)
		conformance.NoError(t, err)
		if _, ok := p.(*consoletelemetry.Provider); !ok {
			t.Fatalf("没设地址时应得到控制台实现，得到 %T", p)
		}
		_ = p.Close()
	}
}
