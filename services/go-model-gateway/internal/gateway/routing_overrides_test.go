package gateway_test

import (
	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/gateway"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/profiles"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
	"testing"
)

func fixedRouting(t *testing.T) *router.RoutingOverrides {
	t.Helper()
	catalog, err := profiles.Parse([]byte(testProfiles))
	if err != nil {
		t.Fatal(err)
	}
	o, err := router.ParseRoutingOverrides([]byte(`{"tenants":{"forced":{"plan":{"profile":"test/strong-a@1","tier":"strong@medium"},"act":{"profile":"test/mid-a@1","tier":"mid@low"},"summary":{"profile":"test/small-a@1","tier":"small@low"},"probe":{"profile":"test/small-a@1","tier":"small@low"},"embedding":{"profile":"test/embed-a@1","tier":"embed@default"}}}}`), catalog)
	if err != nil {
		t.Fatal(err)
	}
	return o
}

func TestConfiguredRoleOverridesPlannerTierAndRecordsActualServedModel(t *testing.T) {
	h, _, store := budgetHarnessStore(t, 100_000_000, func(o *gateway.Options) { o.RoutingOverrides = fixedRouting(t) })
	for _, tc := range []struct {
		role                commonv1.Role
		node, profile, tier string
	}{
		{commonv1.Role_ROLE_PLAN, "plan", "test/strong-a@1", "strong@medium"},
		{commonv1.Role_ROLE_ACT, "read-node", "test/mid-a@1", "mid@low"},
		{commonv1.Role_ROLE_ACT, "child/deliver-node", "test/mid-a@1", "mid@low"},
		{commonv1.Role_ROLE_TRANSFORM, "node#summary", "test/small-a@1", "small@low"},
		{commonv1.Role_ROLE_JUDGE, "node#probe", "test/small-a@1", "small@low"},
	} {
		cc := call(tc.role)
		cc.Tier = "strong@high"
		cc.NodeId = tc.node
		response := h.mustGenerate(t, "forced", cc, say("test"))
		if response.Model.ModelProfileId != tc.profile || response.Model.Tier != tc.tier {
			t.Fatalf("served choice %+v", response.Model)
		}
		if cc.Tier != "strong@high" {
			t.Fatal("mutated caller request")
		}
	}
	embed, err := h.client.Embed(ctx, connect.NewRequest(&mg.EmbedRequest{Tenant: tenant("forced"), Call: call(commonv1.Role_ROLE_FETCH), Texts: []string{"embed"}}))
	if err != nil || embed.Msg.Model.ModelProfileId != "test/embed-a@1" {
		t.Fatalf("embed: %v %v", embed, err)
	}
	rows := usageRows(t, store, "forced")
	if len(rows) != 6 {
		t.Fatalf("attempt ledger rows %d", len(rows))
	}
	want := map[string]int{"test/strong-a@1": 1, "test/mid-a@1": 2, "test/small-a@1": 2, "test/embed-a@1": 1}
	for _, row := range rows {
		want[row["model_profile_id"].(string)]--
		if row["actual_usd_micros"] == nil {
			t.Fatal("actual usage lost")
		}
	}
	for id, n := range want {
		if n != 0 {
			t.Fatalf("actual ledger model %s differs: %d", id, n)
		}
	}
}

func TestConfiguredRoutesRejectPinAndMissingRoleBeforeAnyProviderAttempt(t *testing.T) {
	h := newHarnessOpts(t, nil, func(o *gateway.Options) { o.RoutingOverrides = fixedRouting(t) })
	for _, cc := range []*mg.CallContext{{Role: commonv1.Role_ROLE_ACT, PinnedModelProfile: "test/mid-b@1"}, {Role: commonv1.Role_ROLE_DELIVER}, {Role: commonv1.Role_ROLE_UNSPECIFIED}} {
		_, err := h.generate(t, "forced", cc, say("test"))
		if err == nil {
			t.Fatal("unfrozen route allowed")
		}
	}
	if len(h.fake.allCalls()) != 0 {
		t.Fatal("invalid route reached provider")
	}
	cc := call(commonv1.Role_ROLE_ACT)
	cc.Tier = "strong@high"
	cc.PinnedModelProfile = "test/strong-b@1"
	got := h.mustGenerate(t, "normal", cc, say("legacy"))
	if got.Model.ModelProfileId != "test/strong-b@1" || got.Model.Tier != "strong@high" {
		t.Fatal("unconfigured tenant changed")
	}
}

func TestRequiredRouteNeverFallsBackToAnotherProvider(t *testing.T) {
	for _, category := range []providererr.Category{providererr.Transient, providererr.NotFound} {
		t.Run(category.String(), func(t *testing.T) {
			h := newHarnessOpts(t, nil, func(o *gateway.Options) { o.RoutingOverrides = fixedRouting(t) })
			failure := providererr.New(category, "model_not_found", "injected provider failure")
			h.fake.failNext("mid-a", failure, failure, failure)
			_, err := h.generate(t, "forced", call(commonv1.Role_ROLE_ACT), say("test"))
			if err == nil {
				t.Fatal("provider failure silently fell back")
			}
			calls := h.fake.allCalls()
			if len(calls) == 0 {
				t.Fatal("missing required provider attempt")
			}
			for _, c := range calls {
				if c.Model != "mid-a" {
					t.Fatalf("escaped hard route: %+v", calls)
				}
			}
		})
	}
}
