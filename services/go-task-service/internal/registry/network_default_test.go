package registry_test

import (
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
	"testing"
)

func TestNewTenantInheritsUpdatedGlobalNetworkCapabilities(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.mustPublish(nil, kindPolicy, registry.DefaultTenantPolicy, policyDoc)
		updated := `{"effect_ceiling":"write_reversible","model_allowlist":["a6api/gpt-4o-mini@2026-10"],"sandbox":{"backend":"kata","network":{"allowed":true,"modes":["deny","allowlist","open"],"allow_unlimited":true,"max_grant_minutes":60}}}`
		h.mustPublish(nil, kindPolicy, registry.DefaultTenantPolicy, updated)
		fresh := &commonv1.TenantContext{TenantId: "future-signup-tenant"}
		policy, err := h.getPolicy(fresh, "")
		if err != nil || policy.GetVersion() != "default/v2" {
			t.Fatal(policy, err)
		}
		network := policy.GetDocument().GetValue().GetStructValue().Fields["sandbox"].GetStructValue().Fields["network"].GetStructValue()
		if !network.Fields["allowed"].GetBoolValue() || !network.Fields["allow_unlimited"].GetBoolValue() {
			t.Fatal("fresh signup lost default network capabilities")
		}
		modes := network.Fields["modes"].GetListValue().Values
		if len(modes) != 3 || modes[2].GetStringValue() != "open" {
			t.Fatal(modes)
		}
		if pins := h.mustResolve(fresh, "", ""); pins.GetTenantPolicy() != "default/v2" {
			t.Fatal(pins)
		}
		old, err := h.getPolicy(fresh, "default/v1")
		if err != nil || old.GetDocument().GetValue().GetStructValue().Fields["sandbox"] != nil {
			t.Fatal("old pinned policy was rewritten", err)
		}
	})
}
