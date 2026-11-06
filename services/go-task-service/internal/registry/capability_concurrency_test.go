package registry_test

import (
	"context"
	"encoding/json"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

type pauseCapabilityRead struct {
	relational.Provider
	enabled       atomic.Bool
	ready, resume chan struct{}
}

func (p *pauseCapabilityRead) BindPlatform(purpose string) (relational.Store, error) {
	s, e := p.Provider.BindPlatform(purpose)
	if e != nil {
		return nil, e
	}
	return &pauseCapabilityStore{Store: s, p: p}, nil
}

type pauseCapabilityStore struct {
	relational.Store
	p *pauseCapabilityRead
}

func (s *pauseCapabilityStore) Transaction(ctx context.Context, o relational.TxOptions, f func(context.Context, relational.Tx) error) error {
	return s.Store.Transaction(ctx, o, func(ctx context.Context, tx relational.Tx) error { return f(ctx, &pauseCapabilityTx{Tx: tx, p: s.p}) })
}

type pauseCapabilityTx struct {
	relational.Tx
	p *pauseCapabilityRead
}

func (t *pauseCapabilityTx) QueryOne(ctx context.Context, q string, a ...any) (relational.Row, bool, error) {
	row, found, err := t.Tx.QueryOne(ctx, q, a...)
	if err == nil && found && strings.HasPrefix(q, "SELECT version FROM registry_entries WHERE kind") && strings.Contains(q, "AND state = $3") && t.p.enabled.CompareAndSwap(true, false) {
		close(t.p.ready)
		select {
		case <-t.p.resume:
		case <-ctx.Done():
			return nil, false, ctx.Err()
		}
	}
	return row, found, err
}

// A real PostgreSQL interleaving: the capability transaction reads its baseline,
// another authorized publisher changes the served model/price, then the first
// transaction resumes. The score-only publication must not revert that change.
func TestCapabilityPromotionRejectsConcurrentDefaultChange(t *testing.T) {
	var b backend
	for _, candidate := range backends {
		if candidate.name == "postgres" {
			b = candidate
		}
	}
	p := b.open(t)
	gate := &pauseCapabilityRead{Provider: p, ready: make(chan struct{}), resume: make(chan struct{})}
	h := newHarness(t, gate)
	other := newHarness(t, p)
	baseline := h.release(nil, kindProfile, profileID, profileDoc)
	m := map[string]any{"task_family": "general.unclassified", "role": "act", "tier": "low", "score": 0.7273, "source": "capmatrix:fixture:evidence=" + strings.Repeat("a", 64), "at": "2026-11-06"}
	var document map[string]any
	if err := json.Unmarshal([]byte(profileDoc), &document); err != nil {
		t.Fatal(err)
	}
	document["capability_scores"] = []any{m}
	scoreVersion := h.mustPublish(nil, kindProfile, profileID, mustJSON(t, document))
	document["cost"].(map[string]any)["in_per_m"] = 0.9
	newerVersion := other.mustPublish(nil, kindProfile, profileID, mustJSON(t, document))
	proof := mustJSON(t, map[string]any{"kind": "capability_scores", "policy": "capmatrix-publication-v2", "baseline_version": baseline, "report": "r.json", "report_sha256": strings.Repeat("b", 64), "evaluated_at": "2026-11-06T18:00:00Z", "passed": true, "measurements": []any{map[string]any{"measurement": m, "items": 100, "repeats": 1, "complete_repeats": true, "raw_success_rate": 0.75, "ci95": []float64{0.67, 0.83}, "contamination_rate": 0.01, "eligible_candidates": 10, "total_candidates": 13, "evidence_sha256": strings.Repeat("a", 64), "cost_known": true}}})
	gate.enabled.Store(true)
	result := make(chan error, 1)
	go func() { _, err := h.promote(nil, kindProfile, profileID, scoreVersion, proof); result <- err }()
	select {
	case <-gate.ready:
	case <-time.After(10 * time.Second):
		close(gate.resume)
		t.Fatal("capability promotion did not reach baseline read")
	}
	_, otherErr := other.promote(nil, kindProfile, profileID, newerVersion, passingEvidence)
	close(gate.resume)
	if otherErr != nil {
		t.Fatal(otherErr)
	}
	var err error
	select {
	case err = <-result:
	case <-time.After(10 * time.Second):
		t.Fatal("capability promotion did not complete")
	}
	wantCode(t, "concurrent baseline changed", err, connect.CodeInvalidArgument)
	got, getErr := other.client.GetModelProfile(context.Background(), connect.NewRequest(&registryv1.GetModelProfileRequest{Tenant: t1, Id: profileID}))
	if getErr != nil {
		t.Fatal(getErr)
	}
	if got.Msg.GetModelProfile().GetVersion() != newerVersion {
		t.Fatalf("concurrent release was overwritten: %s", got.Msg.GetModelProfile().GetVersion())
	}
}
