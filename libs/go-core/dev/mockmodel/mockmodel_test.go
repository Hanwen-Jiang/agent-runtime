package mockmodel_test

import (
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/modelprovidertest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/mockmodel"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

type harness struct {
	provider *mockmodel.Provider
	faults   *testkit.Faults
}

func (h *harness) Provider() modelprovider.Provider { return h.provider }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}
func (h *harness) Model() string          { return mockmodel.GenerateModel }
func (h *harness) EmbeddingModel() string { return mockmodel.EmbeddingModel }

func (h *harness) Calls() ([]modelprovidertest.RecordedCall, error) {
	var calls []modelprovidertest.RecordedCall
	for _, c := range h.provider.Calls() {
		calls = append(calls, modelprovidertest.RecordedCall{TenantID: c.TenantID, Method: c.Method})
	}
	return calls, nil
}

func TestConformance(t *testing.T) {
	modelprovidertest.Run(t, func(t *testing.T) modelprovidertest.Harness {
		h := &harness{faults: testkit.NewFaults()}
		h.provider = mockmodel.New(mockmodel.Options{Faults: h.faults})
		t.Cleanup(func() { _ = h.provider.Close() })
		return h
	})
}
