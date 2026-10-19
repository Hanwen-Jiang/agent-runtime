package sqlitestore_test

import (
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/relationaltest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

type harness struct {
	provider *sqlitestore.Provider
	faults   *testkit.Faults
}

func (h *harness) Provider() relational.Provider { return h.provider }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}

func TestConformance(t *testing.T) {
	relationaltest.Run(t, func(t *testing.T) relationaltest.Harness {
		h := &harness{faults: testkit.NewFaults()}
		h.provider = conformance.Must(sqlitestore.New(sqlitestore.Options{Dir: t.TempDir(), Faults: h.faults}))
		t.Cleanup(func() { _ = h.provider.Close() })
		return h
	})
}
