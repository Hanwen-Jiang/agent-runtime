// Package telemetrytest 是 Telemetry 的一致性测试，与 Python 侧 agentruntime_core.conformance.telemetry 一一对应。
package telemetrytest

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

const (
	traceID     = "4bf92f3577b34da6a3ce929d0e0e4736"
	traceparent = "00-" + traceID + "-00f067aa0ba902b7-01"
	secretValue = "sk-live-conformance-123"
	sessionID   = "ses_01JA8Q2N4H6K8M0P2R4T6V8X0Z"
)

// ExportedSpan 等是 harness 交回的已导出记录。
type ExportedSpan struct {
	Name, TraceID, SpanID, ParentSpanID string
	Attributes                          map[string]any
	// Status 为 ok 或 error。
	Status string
	Events []string
}

type ExportedMetric struct {
	// Kind 为 counter 或 histogram。
	Name, Kind string
	Value      float64
	Attributes map[string]any
}

type ExportedLog struct {
	Level, Message string
	Attributes     map[string]any
}

type Exported struct {
	Spans   []ExportedSpan
	Metrics []ExportedMetric
	Logs    []ExportedLog
}

// Harness 由实现所在包提供。故障注入的操作名 export 表示导出失败。
type Harness interface {
	Provider() telemetry.Provider
	conformance.Faultable
	// Exported 返回 Flush 之后已导出的全部记录（含控制台输出等任何落地形式）。
	Exported() Exported
}

func attr(k string, v any) telemetry.Attribute { return telemetry.Attribute{Key: k, Value: v} }

// Run 跑完整的 Telemetry 一致性测试。
func Run(t *testing.T, newHarness func(t *testing.T) Harness) {
	conformance.RunBindingRules(t, func(t *testing.T) conformance.Binder[telemetry.Telemetry] { return newHarness(t).Provider() })
	ctx := context.Background()
	setup := func(t *testing.T) (Harness, telemetry.Telemetry) {
		h := newHarness(t)
		return h, conformance.Must(h.Provider().Bind(conformance.TenantA()))
	}
	span := func(t *testing.T, tel telemetry.Telemetry, ctx context.Context, name string, attrs ...telemetry.Attribute) (context.Context, telemetry.Span) {
		t.Helper()
		c, s, err := tel.StartSpan(ctx, name, attrs...)
		conformance.NoError(t, err)
		return c, s
	}

	conformance.Case(t, "RecordsCarryTenantAttributes", func(t *testing.T) {
		h := newHarness(t)
		tel := conformance.Must(h.Provider().Bind(&commonv1.TenantContext{
			TenantId: conformance.TenantAID, UserId: "u-alice", WorkspaceId: "w-1", SessionId: sessionID,
		}))
		_, s := span(t, tel, ctx, "runtime.round")
		conformance.NoError(t, tel.Count(ctx, "rounds", 1))
		conformance.NoError(t, tel.Log(ctx, telemetry.LevelInfo, "round started"))
		s.End()
		conformance.NoError(t, tel.Flush(ctx))
		e := h.Exported()
		want := map[string]any{"tenant_id": conformance.TenantAID, "user_id": "u-alice", "workspace_id": "w-1", "session_id": sessionID}
		for _, attrs := range []map[string]any{e.Spans[0].Attributes, e.Metrics[0].Attributes, e.Logs[0].Attributes} {
			for k, v := range want {
				if attrs[k] != v {
					t.Fatalf("%s = %v, want %v", k, attrs[k], v)
				}
			}
		}
	})
	conformance.Case(t, "TenantAttributesCannotBeForged", func(t *testing.T) {
		h, tel := setup(t)
		forged := []telemetry.Attribute{attr("tenant_id", conformance.TenantBID), attr("user_id", "u-mallory")}
		_, s := span(t, tel, ctx, "node", forged...)
		s.SetAttributes(attr("tenant_id", conformance.TenantBID))
		s.End()
		conformance.NoError(t, tel.Count(ctx, "calls", 1, forged...))
		conformance.NoError(t, tel.Log(ctx, telemetry.LevelInfo, "x", forged...))
		conformance.NoError(t, tel.Flush(ctx))
		e := h.Exported()
		for _, attrs := range []map[string]any{e.Spans[0].Attributes, e.Metrics[0].Attributes, e.Logs[0].Attributes} {
			if attrs["tenant_id"] != conformance.TenantAID || attrs["user_id"] != "u-alice" {
				t.Fatalf("tenant attributes forged: %v", attrs)
			}
		}
	})
	conformance.Case(t, "PlatformScopeHasNoTenantAttributes", func(t *testing.T) {
		h := newHarness(t)
		tel := conformance.Must(h.Provider().BindPlatform("conformance"))
		_, s := span(t, tel, ctx, "worker.startup")
		s.End()
		conformance.NoError(t, tel.Flush(ctx))
		attrs := h.Exported().Spans[0].Attributes
		if attrs["scope"] != "platform" {
			t.Fatalf("scope = %v", attrs["scope"])
		}
		if _, ok := attrs["tenant_id"]; ok {
			t.Fatal("platform span carries tenant_id")
		}
	})
	conformance.Case(t, "RootSpanJoinsIncomingTrace", func(t *testing.T) {
		h := newHarness(t)
		tel := conformance.Must(h.Provider().Bind(&commonv1.TenantContext{
			TenantId: conformance.TenantAID, Trace: &commonv1.TraceContext{Traceparent: traceparent},
		}))
		_, s := span(t, tel, ctx, "invoke_workflow")
		if s.TraceID() != traceID {
			t.Fatalf("trace id = %s", s.TraceID())
		}
		s.End()
		conformance.NoError(t, tel.Flush(ctx))
		if h.Exported().Spans[0].TraceID != traceID {
			t.Fatal("exported trace id differs")
		}
	})
	conformance.Case(t, "NestedSpansFormATree", func(t *testing.T) {
		h, tel := setup(t)
		parentCtx, parent := span(t, tel, ctx, "runtime.round")
		_, child := span(t, tel, parentCtx, "execute_tool")
		if child.TraceID() != parent.TraceID() {
			t.Fatal("child in another trace")
		}
		child.End()
		parent.End()
		conformance.NoError(t, tel.Flush(ctx))
		byName := map[string]ExportedSpan{}
		for _, s := range h.Exported().Spans {
			byName[s.Name] = s
		}
		if byName["execute_tool"].ParentSpanID != byName["runtime.round"].SpanID {
			t.Fatal("parent link missing")
		}
	})
	conformance.Case(t, "RecordErrorMarksSpan", func(t *testing.T) {
		h, tel := setup(t)
		_, s := span(t, tel, ctx, "node")
		s.RecordError(fmt.Errorf("boom: %w", errors.New("tool exploded")))
		s.End()
		conformance.NoError(t, tel.Flush(ctx))
		if h.Exported().Spans[0].Status != "error" {
			t.Fatal("span not marked as error")
		}
	})
	conformance.Case(t, "SensitiveKeysAndSecretValuesAreRedacted", func(t *testing.T) {
		h, tel := setup(t)
		keys := []string{"password", "db_secret", "access_token", "api_key", "x-apikey", "Authorization", "credential", "cookie"}
		var attrs []telemetry.Attribute
		for _, k := range keys {
			attrs = append(attrs, attr(k, secretValue))
		}
		attrs = append(attrs, attr("tool_input", secret.New(secretValue)))
		_, s := span(t, tel, ctx, "node", attrs...)
		s.AddEvent("called", attr("arg", secret.New(secretValue)))
		s.End()
		conformance.NoError(t, tel.Count(ctx, "calls", 1, attr("key", secret.New(secretValue))))
		conformance.NoError(t, tel.Log(ctx, telemetry.LevelWarning, fmt.Sprintf("calling with %v", secret.New(secretValue)), attr("arg", secret.New(secretValue))))
		conformance.NoError(t, tel.Flush(ctx))
		e := h.Exported()
		if strings.Contains(fmt.Sprintf("%#v", e), secretValue) {
			t.Fatal("secret leaked into exported telemetry")
		}
		for _, k := range append(keys, "tool_input") {
			if e.Spans[0].Attributes[k] != telemetry.Redacted {
				t.Fatalf("%s not redacted: %v", k, e.Spans[0].Attributes[k])
			}
		}
	})
	conformance.Case(t, "MetricsRecordValues", func(t *testing.T) {
		h, tel := setup(t)
		conformance.NoError(t, tel.Count(ctx, "node.calls", 2, attr("role", "fetch")))
		conformance.NoError(t, tel.Observe(ctx, "node.latency_ms", 12.5))
		conformance.NoError(t, tel.Flush(ctx))
		byName := map[string]ExportedMetric{}
		for _, m := range h.Exported().Metrics {
			byName[m.Name] = m
		}
		if m := byName["node.calls"]; m.Kind != "counter" || m.Value != 2 || m.Attributes["role"] != "fetch" {
			t.Fatalf("counter = %+v", m)
		}
		if m := byName["node.latency_ms"]; m.Kind != "histogram" || m.Value != 12.5 {
			t.Fatalf("histogram = %+v", m)
		}
	})
	conformance.Case(t, "EmptyNamesAreInvalid", func(t *testing.T) {
		_, tel := setup(t)
		_, _, err := tel.StartSpan(ctx, "")
		conformance.ExpectError(t, err, providererr.Invalid, "")
		conformance.ExpectError(t, tel.Count(ctx, "", 1), providererr.Invalid, "")
	})
	conformance.Case(t, "ExportFailureDoesNotBreakCallers", func(t *testing.T) {
		h, tel := setup(t)
		conformance.SkipIfUnsupported(t, h.InjectFault("export", providererr.Transient))
		_, s := span(t, tel, ctx, "node")
		conformance.NoError(t, tel.Count(ctx, "calls", 1))
		conformance.NoError(t, tel.Log(ctx, telemetry.LevelInfo, "still fine"))
		s.End()
		if err := tel.Flush(ctx); err != nil && !providererr.IsRetryable(err) {
			t.Fatalf("export failure must be transient, got %v", err)
		}
	})
}
