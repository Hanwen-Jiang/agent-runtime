package memcache_test

import (
	"context"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/cachetest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

type harness struct {
	provider cache.Provider
	clock    *testkit.FakeClock
	faults   *testkit.Faults
}

func (h *harness) Provider() cache.Provider                   { return h.provider }
func (h *harness) Advance(_ context.Context, d time.Duration) { h.clock.Advance(d) }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}

func TestConformance(t *testing.T) {
	cachetest.Run(t, func(t *testing.T) cachetest.Harness {
		h := &harness{clock: testkit.NewFakeClock(), faults: testkit.NewFaults()}
		h.provider = memcache.New(memcache.Options{Clock: h.clock, Faults: h.faults})
		t.Cleanup(func() { _ = h.provider.Close() })
		return h
	})
}
