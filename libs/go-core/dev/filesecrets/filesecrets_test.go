package filesecrets_test

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/secretstoretest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

const maxTTL = time.Hour

type harness struct {
	provider *filesecrets.Provider
	clock    *testkit.FakeClock
	faults   *testkit.Faults
}

func (h *harness) Provider() secretstore.Provider             { return h.provider }
func (h *harness) Advance(_ context.Context, d time.Duration) { h.clock.Advance(d) }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}
func (h *harness) MaxTTL() time.Duration { return maxTTL }
func (h *harness) SeedSecret(tenantID, name, value string) {
	h.provider.SeedSecret(tenantID, name, value)
}
func (h *harness) GrantRole(tenantID, role string) { h.provider.GrantRole(tenantID, role) }

func TestConformance(t *testing.T) {
	secretstoretest.Run(t, func(t *testing.T) secretstoretest.Harness {
		h := &harness{clock: testkit.NewFakeClock(), faults: testkit.NewFaults()}
		h.provider = conformance.Must(filesecrets.New(filesecrets.Options{
			Path: filepath.Join(t.TempDir(), "secrets.json"), MaxTTL: maxTTL, Clock: h.clock, Faults: h.faults,
		}))
		t.Cleanup(func() { _ = h.provider.Close() })
		return h
	})
}
