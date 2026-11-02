package router_test

import (
	"encoding/json"
	"math"
	"slices"
	"testing"
	"time"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-model-gateway/internal/router"
)

var rankingNow = time.Date(2026, 11, 3, 0, 0, 0, 0, time.UTC)

func rankedProfile(t *testing.T, id, vendor, released string, price float64, extra map[string]any) string {
	t.Helper()
	var p map[string]any
	if err := json.Unmarshal([]byte(profileJSON(id, vendor, "mid", low, full, price, 0)), &p); err != nil {
		t.Fatal(err)
	}
	if released != "" {
		p["released_at"] = released
	}
	for k, v := range extra {
		p[k] = v
	}
	b, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func measured(family, role, tier string, score float64) map[string]any {
	return map[string]any{"task_family": family, "role": role, "tier": tier, "score": score, "source": "synthetic:test", "at": "2026-11-01"}
}

func rankRouter(t *testing.T, items []string, allow router.Allowlist) *router.Router {
	t.Helper()
	return router.NewWithClock(catalogOf(t, items), allow, func() time.Time { return rankingNow })
}

func TestReleaseAgeOutweighsCheapOldModel(t *testing.T) {
	r := rankRouter(t, []string{
		rankedProfile(t, "x/old", "openai", "2024-07-18", 0.01, nil),
		rankedProfile(t, "x/recent", "google", "2026-10-01", 10, nil),
	}, nil)
	c := mustSelect(t, r, router.Request{Kind: router.KindGenerate})
	if ids(c)[0] != "x/recent" {
		t.Fatalf("recent model should beat cheap old model: %v", ids(c))
	}
	if !c.AsOf.Equal(rankingNow) {
		t.Fatalf("clock not captured: %v", c.AsOf)
	}
	s := c.Candidates[0].Score
	if math.Abs(s.Total-(0.45*s.Capability+0.35*s.Recency+0.15*s.Value+0.05*s.Reliability)) > 1e-12 {
		t.Fatalf("unexplained score: %+v", s)
	}
	if !slices.Equal(ids(c), ids(mustSelect(t, r, router.Request{Kind: router.KindGenerate}))) {
		t.Fatal("fixed clock must be deterministic")
	}
}

func TestTaskFamilyAndNodeRoleChangePreferredModel(t *testing.T) {
	a := []any{measured("coding.patch", "transform", "low", 0.95), measured("coding.patch", "judge", "low", 0.25), measured("research.report", "transform", "low", 0.2)}
	b := []any{measured("coding.patch", "transform", "low", 0.3), measured("coding.patch", "judge", "low", 0.95), measured("research.report", "transform", "low", 0.9)}
	r := rankRouter(t, []string{
		rankedProfile(t, "x/code", "openai", "2026-10-01", 2, map[string]any{"capability_scores": a}),
		rankedProfile(t, "x/review", "google", "2026-10-01", 1, map[string]any{"capability_scores": b}),
	}, nil)
	for _, tc := range []struct {
		family string
		role   commonv1.Role
		want   string
	}{
		{"coding.patch", commonv1.Role_ROLE_TRANSFORM, "x/code"},
		{"coding.patch", commonv1.Role_ROLE_JUDGE, "x/review"},
		{"research.report", commonv1.Role_ROLE_TRANSFORM, "x/review"},
	} {
		c := mustSelect(t, r, router.Request{Kind: router.KindGenerate, Tier: "mid@low", TaskFamily: tc.family, Role: tc.role})
		if ids(c)[0] != tc.want || !c.Candidates[0].Score.CapabilityMeasured {
			t.Fatalf("%s/%s: %+v", tc.family, tc.role, c)
		}
	}
}

func TestAvailabilityAndTenantFiltersCannotBeBypassedByPin(t *testing.T) {
	items := []string{
		rankedProfile(t, "x/offline", "openai", "2026-11-01", 0, map[string]any{"availability": false}),
		rankedProfile(t, "x/denied", "openai", "2026-11-01", 0, nil),
		rankedProfile(t, "x/allowed", "google", "2026-09-01", 10, nil),
	}
	r := rankRouter(t, items, staticAllowlist{"tenant": {"x/offline", "x/allowed"}})
	for _, pin := range []string{"x/offline", "x/denied"} {
		c := mustSelect(t, r, router.Request{TenantID: "tenant", Kind: router.KindGenerate, PinnedProfile: pin})
		if !slices.Equal(ids(c), []string{"x/allowed"}) || c.PinApplied {
			t.Fatalf("ineligible pin %s: %+v", pin, c)
		}
	}
}

func TestCapabilityDoesNotLeakAcrossReasoningTiers(t *testing.T) {
	r := rankRouter(t, []string{
		rankedProfile(t, "x/specialist", "openai", "2026-10-01", 2, map[string]any{
			"tiers":             []any{map[string]any{"name": "low"}, map[string]any{"name": "high"}},
			"capability_scores": []any{measured("coding.patch", "transform", "high", 0.95)},
		}),
		rankedProfile(t, "x/general", "google", "2026-10-01", 1, map[string]any{
			"tiers": []any{map[string]any{"name": "low"}, map[string]any{"name": "high"}},
		}),
	}, nil)
	for tier, want := range map[string]string{"low": "x/general", "high": "x/specialist"} {
		c := mustSelect(t, r, router.Request{Kind: router.KindGenerate, TaskFamily: "coding.patch", Role: commonv1.Role_ROLE_TRANSFORM, Tier: "mid@" + tier})
		if ids(c)[0] != want {
			t.Fatalf("tier %s: %v", tier, ids(c))
		}
	}
}

func TestRankingRetainsPinAvoidAndUsesReliability(t *testing.T) {
	items := []string{
		rankedProfile(t, "x/flaky", "openai", "2026-10-01", 1, map[string]any{"reliability": map[string]any{"score": 0.1, "source": "test", "at": "2026-11-01"}}),
		rankedProfile(t, "x/reliable", "google", "2026-10-01", 1, map[string]any{"reliability": map[string]any{"score": 0.99, "source": "test", "at": "2026-11-01"}}),
	}
	r := rankRouter(t, items, nil)
	if c := mustSelect(t, r, router.Request{Kind: router.KindGenerate}); ids(c)[0] != "x/reliable" {
		t.Fatalf("reliability ignored: %v", ids(c))
	}
	if c := mustSelect(t, r, router.Request{Kind: router.KindGenerate, PinnedProfile: "x/flaky"}); ids(c)[0] != "x/flaky" || !c.PinApplied {
		t.Fatalf("pin ignored: %+v", c)
	}
	if c := mustSelect(t, r, router.Request{Kind: router.KindGenerate, AvoidVendor: "openai", PinnedProfile: "x/flaky"}); !slices.Equal(ids(c), []string{"x/reliable"}) || c.PinApplied {
		t.Fatalf("avoid ignored: %+v", c)
	}
}

func TestMissingMetadataIsExplicitAndPriceDateIsNotReleaseDate(t *testing.T) {
	r := rankRouter(t, []string{rankedProfile(t, "x/legacy", "openai", "", 0, nil)}, nil)
	c := mustSelect(t, r, router.Request{Kind: router.KindGenerate, TaskFamily: "coding.patch"})
	s := c.Candidates[0].Score
	if s.ReleaseKnown || s.CapabilityMeasured || s.ReliabilityMeasured || s.Recency != 0 || s.Capability != 0.5 || math.IsNaN(s.Total) {
		t.Fatalf("invented metadata or invalid zero-price score: %+v", s)
	}
}
