package router_test

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
	"strings"
	"testing"
)

func TestRequiredProfileCannotFallBackOrBypassExistingFilters(t *testing.T) {
	catalog := catalogOf(t, fixture)
	req := router.Request{TenantID: "forced", Kind: router.KindGenerate, Role: commonv1.Role_ROLE_ACT, Tier: "mid@low", RequiredProfile: "x/mid-deepseek"}
	selected := mustSelect(t, router.New(catalog, nil), req)
	if len(selected.Candidates) != 1 || selected.Candidates[0].Profile.ID != req.RequiredProfile {
		t.Fatal("hard requirement retained fallback", ids(selected))
	}
	req.AvoidVendor = "deepseek"
	selected = mustSelect(t, router.New(catalog, nil), req)
	if len(selected.Candidates) != 1 || !selected.AvoidVendorUnmet {
		t.Fatal("avoid vendor must not broaden hard route")
	}
	_, err := router.New(catalog, staticAllowlist{"forced": {"x/mid-google"}}).Select(context.Background(), req)
	wantCategory(t, "allowlist", err, providererr.NotFound)
	req.Tier = "small@low"
	req.RequiredProfile = "x/small-google"
	req.Tools = true
	_, err = router.New(catalog, nil).Select(context.Background(), req)
	wantCategory(t, "capability", err, providererr.NotFound)
	req.Tier = "mid@low"
	req.RequiredProfile = "x/mid-deepseek"
	req.Tools = false
	unavailable := append([]string(nil), fixture...)
	unavailable[2] = strings.TrimSuffix(unavailable[2], "}") + `,"availability":false}`
	_, err = router.New(catalogOf(t, unavailable), nil).Select(context.Background(), req)
	wantCategory(t, "availability", err, providererr.NotFound)
}

func TestRoutingOverrideStartupValidationAndLogicalRoles(t *testing.T) {
	catalog := catalogOf(t, fixture)
	good := `{"tenants":{"forced":{"act":{"profile":"x/mid-deepseek","tier":"mid@low"},"summary":{"profile":"x/small-openai","tier":"small@low"},"probe":{"profile":"x/small-openai","tier":"small@low"},"embedding":{"profile":"x/embed-openai","tier":"embed@default"}}}}`
	o, err := router.ParseRoutingOverrides([]byte(good), catalog)
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{
		strings.Replace(good, "mid@low", "strong@medium", 1), strings.Replace(good, "mid@low", "mid@missing", 1),
		strings.Replace(good, "x/mid-deepseek", "unknown/profile", 1), strings.Replace(good, `"act":`, `"typo":`, 1),
		strings.Replace(good, `"forced":`, `"../foreign":`, 1), strings.Replace(good, `"profile":`, `"pin":`, 1),
		`{"tenants":{"forced":{}}}`, `{"tenants":null}`, `{"tenant":{}}`, good + `{}`,
	} {
		if _, err := router.ParseRoutingOverrides([]byte(bad), catalog); err == nil {
			t.Fatalf("accepted invalid override %s", bad)
		}
	}
	for _, c := range []struct {
		kind                router.Kind
		role                commonv1.Role
		node, profile, tier string
	}{
		{router.KindGenerate, commonv1.Role_ROLE_ACT, "read-node#step1", "x/mid-deepseek", "mid@low"},
		{router.KindGenerate, commonv1.Role_ROLE_ACT, "child/deliver#step1", "x/mid-deepseek", "mid@low"},
		{router.KindGenerate, commonv1.Role_ROLE_TRANSFORM, "read#summary", "x/small-openai", "small@low"},
		{router.KindGenerate, commonv1.Role_ROLE_JUDGE, "read#probe", "x/small-openai", "small@low"},
		{router.KindEmbed, commonv1.Role_ROLE_FETCH, "mem#probe", "x/embed-openai", "embed@default"},
	} {
		got, err := o.Apply(router.Request{TenantID: "forced", Kind: c.kind, Role: c.role, Tier: "strong@high"}, c.node)
		if err != nil || got.RequiredProfile != c.profile || got.Tier != c.tier {
			t.Fatalf("logical route: %+v %v", got, err)
		}
	}
	if _, err = o.Apply(router.Request{TenantID: "forced", Role: commonv1.Role_ROLE_PLAN}, "planner"); err == nil {
		t.Fatal("missing configured role permitted")
	}
	if _, err = o.Apply(router.Request{TenantID: "forced", Role: commonv1.Role_ROLE_ACT, PinnedProfile: "x/mid-google"}, "act"); err == nil {
		t.Fatal("conflicting pin permitted")
	}
	legacy := router.Request{TenantID: "normal", Role: commonv1.Role_ROLE_PLAN, Tier: "strong@high", PinnedProfile: "x/strong-openai"}
	got, err := o.Apply(legacy, "planner")
	if err != nil || got != legacy {
		t.Fatalf("unconfigured tenant changed: %+v %v", got, err)
	}
}
