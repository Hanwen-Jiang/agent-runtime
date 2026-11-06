package registry_test

import (
	"encoding/json"
	"strings"
	"testing"

	"connectrpc.com/connect"
)

func TestCapabilityScoresPromoteOnlyVerifiedMeasurementsOnUnchangedModels(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		baseline := h.release(nil, kindProfile, profileID, profileDoc)
		measurement := map[string]any{"task_family": "general.unclassified", "role": "act", "tier": "low", "score": 0.7273, "source": "capmatrix:fixture:evidence=" + strings.Repeat("a", 64), "at": "2026-11-06"}
		var candidate map[string]any
		if err := json.Unmarshal([]byte(profileDoc), &candidate); err != nil {
			t.Fatal(err)
		}
		candidate["capability_scores"] = []any{measurement}
		version := h.mustPublish(nil, kindProfile, profileID, mustJSON(t, candidate))
		proof := map[string]any{"measurement": measurement, "items": 100, "repeats": 1, "complete_repeats": true, "raw_success_rate": 0.75, "ci95": []float64{0.67, 0.83}, "contamination_rate": 0.01, "eligible_candidates": 10, "total_candidates": 13, "evidence_sha256": strings.Repeat("a", 64), "cost_known": true}
		evidence := map[string]any{"kind": "capability_scores", "policy": "capmatrix-publication-v2", "baseline_version": baseline, "report": "fixture-report.json", "report_sha256": strings.Repeat("b", 64), "evaluated_at": "2026-11-06T18:00:00Z", "passed": true, "measurements": []any{proof}}
		for _, tc := range []struct {
			field string
			value any
		}{{"items", 39}, {"cost_known", false}, {"complete_repeats", false}, {"ci95", []float64{0.1, 0.9}}, {"eligible_candidates", 6}, {"contamination_rate", 0.06}, {"raw_success_rate", 0.9}} {
			before := proof[tc.field]
			proof[tc.field] = tc.value
			_, err := h.promote(nil, kindProfile, profileID, version, mustJSON(t, evidence))
			wantCode(t, tc.field, err, connect.CodeInvalidArgument)
			proof[tc.field] = before
		}
		// A score certificate never authorizes an untested price or model change.
		candidate["cost"].(map[string]any)["in_per_m"] = 100.0
		priceChanged := h.mustPublish(nil, kindProfile, profileID, mustJSON(t, candidate))
		_, err := h.promote(nil, kindProfile, profileID, priceChanged, mustJSON(t, evidence))
		wantCode(t, "price change", err, connect.CodeInvalidArgument)
		measurement["score"] = 0.9
		_, err = h.promote(nil, kindProfile, profileID, version, mustJSON(t, evidence))
		wantCode(t, "wrong score", err, connect.CodeInvalidArgument)
		measurement["score"] = 0.7273
		if _, err = h.promote(nil, kindProfile, profileID, version, mustJSON(t, evidence)); err != nil {
			t.Fatal(err)
		}
		_, err = h.promote(nil, kindProfile, profileID, priceChanged, mustJSON(t, evidence))
		wantCode(t, "stale baseline", err, connect.CodeInvalidArgument)
	})
}
