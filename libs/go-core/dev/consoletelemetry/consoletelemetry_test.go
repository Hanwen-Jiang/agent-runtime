package consoletelemetry_test

import (
	"bytes"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/telemetrytest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/consoletelemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/telemetry"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

type harness struct {
	provider *consoletelemetry.Provider
	out      *bytes.Buffer
	faults   *testkit.Faults
}

func (h *harness) Provider() telemetry.Provider { return h.provider }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}

func (h *harness) Exported() telemetrytest.Exported {
	var e telemetrytest.Exported
	for _, s := range h.provider.Spans() {
		e.Spans = append(e.Spans, telemetrytest.ExportedSpan{
			Name: s.Name, TraceID: s.TraceID, SpanID: s.SpanID, ParentSpanID: s.ParentSpanID,
			Attributes: s.Attributes, Status: s.Status, Events: s.Events,
		})
	}
	for _, m := range h.provider.Metrics() {
		e.Metrics = append(e.Metrics, telemetrytest.ExportedMetric{Name: m.Name, Kind: m.Kind, Value: m.Value, Attributes: m.Attributes})
	}
	for _, l := range h.provider.Logs() {
		e.Logs = append(e.Logs, telemetrytest.ExportedLog{Level: l.Level, Message: l.Message, Attributes: l.Attributes})
	}
	// 控制台输出也必须不含明文，一并放进记录里参与检查。
	e.Logs = append(e.Logs, telemetrytest.ExportedLog{Level: "console", Message: h.out.String()})
	return e
}

func TestConformance(t *testing.T) {
	telemetrytest.Run(t, func(t *testing.T) telemetrytest.Harness {
		h := &harness{out: &bytes.Buffer{}, faults: testkit.NewFaults()}
		h.provider = consoletelemetry.New(consoletelemetry.Options{Writer: h.out, Faults: h.faults})
		t.Cleanup(func() { _ = h.provider.Close() })
		return h
	})
}
