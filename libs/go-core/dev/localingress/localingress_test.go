package localingress_test

import (
	"context"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/ingresstest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

const (
	limit  = 3
	window = time.Minute
)

type harness struct {
	provider *localingress.Provider
	clock    *testkit.FakeClock
}

func (h *harness) Provider() ingress.Ingress                  { return h.provider }
func (h *harness) Advance(_ context.Context, d time.Duration) { h.clock.Advance(d) }
func (h *harness) IssueToken(tenantID, userID string, ttl time.Duration) string {
	return h.provider.IssueToken(tenantID, userID, ttl)
}
func (h *harness) RateLimit() (int, time.Duration) { return limit, window }

func TestConformance(t *testing.T) {
	ingresstest.Run(t, func(t *testing.T) ingresstest.Harness {
		h := &harness{clock: testkit.NewFakeClock()}
		h.provider = localingress.New(localingress.Options{
			SigningKey: []byte("conformance-signing-key"), Clock: h.clock, Limit: limit, Window: window,
		})
		t.Cleanup(func() { _ = h.provider.Close() })
		return h
	})
}
