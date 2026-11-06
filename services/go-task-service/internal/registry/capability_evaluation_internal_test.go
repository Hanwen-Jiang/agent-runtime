package registry

import (
	"math"
	"reflect"
	"testing"
)

func TestCapabilityComparisonPreservesLargeIntegers(t *testing.T) {
	a, _, err := capabilityScores(`{"constraints":{"context_window":9007199254740992},"tiers":[{"thinking_budget":9007199254740992}],"capability_scores":[]}`)
	if err != nil {
		t.Fatal(err)
	}
	b, _, err := capabilityScores(`{"constraints":{"context_window":9007199254740993},"tiers":[{"thinking_budget":9007199254740993}],"capability_scores":[]}`)
	if err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(a, b) {
		t.Fatal("non-score integer change was hidden by float64 rounding")
	}
}
func TestCapabilityCoverageCannotOverflow(t *testing.T) {
	for _, tc := range []struct {
		eligible, total int
		want            bool
	}{{1, math.MaxInt, false}, {math.MaxInt / 2, math.MaxInt, false}, {math.MaxInt/2 + 1, math.MaxInt, true}, {6, 13, false}, {7, 13, true}, {0, 0, false}, {2, 1, false}} {
		if got := capabilityCoverage(tc.eligible, tc.total); got != tc.want {
			t.Fatalf("coverage %d/%d = %v, want %v", tc.eligible, tc.total, got, tc.want)
		}
	}
}
