package registry_test

import (
	"encoding/json"
	"testing"

	"connectrpc.com/connect"
)

func TestModelRoutingMetadataAtPublish(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		for _, name := range []string{"valid", "unknown field", "unknown tier", "duplicate dimension"} {
			var doc map[string]any
			if err := json.Unmarshal([]byte(profileDoc), &doc); err != nil {
				t.Fatal(err)
			}
			entry := map[string]any{"task_family": "coding.patch", "role": "transform", "tier": "low", "score": 0.9, "source": "synthetic:registry-test", "at": "2026-11-01"}
			doc["released_at"] = "2026-10-01"
			doc["availability"] = true
			doc["capability_scores"] = []any{entry}
			switch name {
			case "unknown field":
				entry["socre"] = 0.8
			case "unknown tier":
				entry["tier"] = "high"
			case "duplicate dimension":
				second := map[string]any{"task_family": "coding.patch", "role": "transform", "tier": "low", "score": 0.8, "source": "synthetic:another-eval", "at": "2026-11-02"}
				doc["capability_scores"] = []any{entry, second}
			}
			_, err := h.publish(nil, kindProfile, profileID, mustJSON(t, doc))
			if name == "valid" {
				if err != nil {
					t.Fatalf("valid routing metadata: %v", err)
				}
			} else {
				wantCode(t, name, err, connect.CodeInvalidArgument)
			}
		}
	})
}
