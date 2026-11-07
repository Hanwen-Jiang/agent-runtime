package registry

import (
	"math"
	"reflect"
	"strings"
	"testing"
)

func TestCapabilityComparisonUsesExactNumericValues(t *testing.T) {
	for _, tc := range []struct {
		left, right string
		equal       bool
	}{
		{`4`, `4.0`, true},
		{`1000000`, `1e6`, true},
		{`1.000E+06`, `1000000.0`, true},
		{`-0`, `0.0e12345`, true},
		{`-1.2500`, `-125e-2`, true},
		{`9007199254740993`, `9007199254740993.0`, true},
		{`9007199254740992`, `9007199254740993`, false},
		{`0.1`, `0.10000000000000001`, false},
		{`4.0`, `4.00000000000000001`, false},
		{`1e1000000000`, `10e999999999`, true},
		{`1e-1000000000`, `0.1e-999999999`, true},
		{`1e999999999999999999999999999`, `10e999999999999999999999999998`, true},
		{`1e999999999999999999999999999`, `1e999999999999999999999999998`, false},
		{`4`, `"4"`, false},
		{`4`, `true`, false},
		{`4`, `null`, false},
	} {
		t.Run(tc.left+"/"+tc.right, func(t *testing.T) {
			// Nested maps and arrays exercise the comparison used by Profile
			// pricing, constraints and tier configuration together.
			doc := `{"cost":{"in_per_m":NUMBER},"tiers":[{"thinking_budget":NUMBER}],"capability_scores":[]}`
			left, _, err := capabilityScores(strings.ReplaceAll(doc, "NUMBER", tc.left))
			if err != nil {
				t.Fatal(err)
			}
			right, _, err := capabilityScores(strings.ReplaceAll(doc, "NUMBER", tc.right))
			if err != nil {
				t.Fatal(err)
			}
			if got := reflect.DeepEqual(left, right); got != tc.equal {
				t.Fatalf("comparison = %v, want %v", got, tc.equal)
			}
		})
	}
	for _, pair := range [][2]string{
		{`{"tiers":[1,2]}`, `{"tiers":[2,1]}`},
		{`{"cost":{"value":4}}`, `{"cost":{"other":4}}`},
		{`{"vendor":"first"}`, `{"vendor":"second"}`},
	} {
		left, _, err := capabilityScores(pair[0])
		if err != nil {
			t.Fatal(err)
		}
		right, _, err := capabilityScores(pair[1])
		if err != nil {
			t.Fatal(err)
		}
		if reflect.DeepEqual(left, right) {
			t.Fatalf("non-numeric structure changed without rejection: %v", pair)
		}
	}
}

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
