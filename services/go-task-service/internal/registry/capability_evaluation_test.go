package registry_test

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
	"reflect"
	"strings"
	"testing"

	"connectrpc.com/connect"
)

func TestCapabilityPromotionAcceptsEquivalentSeedAndWireNumbers(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		// A seed preserves decimal spelling; publishing via protobuf Struct
		// serializes equivalent values differently, as in the live MX report.
		seed := strings.NewReplacer(
			`"in_per_m":0.15`, `"in_per_m":0.1500`,
			`"out_per_m":0.6`, `"out_per_m":6e-1`,
			`"cache_read_per_m":0.075`, `"cache_read_per_m":0.0750`,
			`"context_window":128000`, `"context_window":1.28e5`,
			`"max_output":16384`, `"max_output":16384.00`,
			`"thinking_budget":0`, `"thinking_budget":-0.0`,
		).Replace(profileDoc)
		seeded, err := h.reg.Seed(t.Context(), []registry.SeedEntry{{Kind: "model_profile", Name: profileID, Document: json.RawMessage(seed)}})
		if err != nil {
			t.Fatal(err)
		}
		baseline := seeded[0].Version
		before := registryRows(t, h, `SELECT document,evaluation FROM registry_entries WHERE kind=$1 AND name=$2 AND version=1`, int64(kindProfile), profileID)
		version, evidence := capabilityCandidate(t, h, baseline, "exact-numeric-values")
		if _, err := h.promote(nil, kindProfile, profileID, version, evidence); err != nil {
			t.Fatal(err)
		}
		after := registryRows(t, h, `SELECT document,evaluation FROM registry_entries WHERE kind=$1 AND name=$2 AND version=1`, int64(kindProfile), profileID)
		if !reflect.DeepEqual(before, after) {
			t.Fatal("comparison rewrote the immutable baseline document or evidence")
		}
	})
}

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

func capabilityCandidate(t *testing.T, h *harness, baseline, source string) (string, string) {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal([]byte(profileDoc), &doc); err != nil {
		t.Fatal(err)
	}
	measurement := map[string]any{"task_family": "general.unclassified", "role": "act", "tier": "low", "score": 0.7273, "source": "capmatrix:" + source + ":evidence=" + strings.Repeat("a", 64), "at": "2026-11-06"}
	doc["capability_scores"] = []any{measurement}
	version := h.mustPublish(nil, kindProfile, profileID, mustJSON(t, doc))
	evidence := map[string]any{"kind": "capability_scores", "policy": "capmatrix-publication-v2", "baseline_version": baseline, "report": source + ".json", "report_sha256": strings.Repeat("b", 64), "evaluated_at": "2026-11-06T18:00:00Z", "passed": true, "measurements": []any{map[string]any{"measurement": measurement, "items": 100, "repeats": 1, "complete_repeats": true, "raw_success_rate": 0.75, "ci95": []float64{0.67, 0.83}, "contamination_rate": 0.01, "eligible_candidates": 10, "total_candidates": 13, "evidence_sha256": strings.Repeat("a", 64), "cost_known": true}}}
	return version, mustJSON(t, evidence)
}

func registryRows(t *testing.T, h *harness, sql string, args ...any) []relational.Row {
	t.Helper()
	store, err := h.store.BindPlatform("registry")
	if err != nil {
		t.Fatal(err)
	}
	var rows []relational.Row
	err = store.Transaction(context.Background(), relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var e error
		rows, e = tx.Query(ctx, sql, args...)
		return e
	})
	if err != nil {
		t.Fatal(err)
	}
	return rows
}

func TestCapabilityPublishedEvidenceIsImmutableAndRetryIsReadOnly(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		baseline := h.release(nil, kindProfile, profileID, profileDoc)
		version, evidence := capabilityCandidate(t, h, baseline, "first")
		if _, err := h.promote(nil, kindProfile, profileID, version, evidence); err != nil {
			t.Fatal(err)
		}
		before := registryRows(t, h, "SELECT version,evaluation,updated_at,state FROM registry_entries ORDER BY version")
		replay, err := h.promote(nil, kindProfile, profileID, version, evidence)
		if err != nil || replay.PreviousDefaultVersion != baseline {
			t.Fatalf("exact replay failed: %v %v", replay, err)
		}
		if after := registryRows(t, h, "SELECT version,evaluation,updated_at,state FROM registry_entries ORDER BY version"); !reflect.DeepEqual(before, after) {
			t.Fatal("idempotent replay rewrote published evidence or timestamps")
		}
		changed := strings.Replace(evidence, "first.json", "changed-report.json", 1)
		_, err = h.promote(nil, kindProfile, profileID, version, changed)
		wantCode(t, "changed certificate", err, connect.CodeInvalidArgument)
		next, nextEvidence := capabilityCandidate(t, h, version, "second")
		if _, err = h.promote(nil, kindProfile, profileID, next, nextEvidence); err != nil {
			t.Fatal(err)
		}
		_, err = h.promote(nil, kindProfile, profileID, version, evidence)
		wantCode(t, "retired revision", err, connect.CodeInvalidArgument)
		rows := registryRows(t, h, "SELECT evaluation FROM registry_entries WHERE version=$1", int64(2))
		if rows[0]["evaluation"] != before[1]["evaluation"] {
			t.Fatal("retired evidence was overwritten")
		}
	})
}

func TestCapabilityConcurrentPromotionsKeepOneDefaultAndRejectStaleBaseline(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		baseline := h.release(nil, kindProfile, profileID, profileDoc)
		const parallel = 8
		requests := make([]*connect.Request[registryv1.PromoteRequest], parallel)
		for i := range parallel {
			version, evidence := capabilityCandidate(t, h, baseline, fmt.Sprintf("parallel-%d", i))
			requests[i] = withToken(&registryv1.PromoteRequest{Kind: kindProfile, Name: profileID, Version: version, Evaluation: payload(t, evidence)}, publishToken)
		}
		start := make(chan struct{})
		results := make(chan error, parallel)
		for _, request := range requests {
			go func(request *connect.Request[registryv1.PromoteRequest]) {
				<-start
				_, err := h.client.Promote(context.Background(), request)
				results <- err
			}(request)
		}
		close(start)
		successes := 0
		for range parallel {
			if err := <-results; err == nil {
				successes++
			} else if connect.CodeOf(err) != connect.CodeInvalidArgument {
				t.Fatalf("unexpected concurrency failure: %v", err)
			}
		}
		if successes != 1 {
			t.Fatalf("same baseline authorized %d concurrent publications", successes)
		}
		rows := registryRows(t, h, "SELECT version FROM registry_entries WHERE state=$1", int64(stateDefault))
		if len(rows) != 1 {
			t.Fatalf("multiple defaults after concurrency: %v", rows)
		}
	})
}
