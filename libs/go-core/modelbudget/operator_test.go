package modelbudget

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestOperatorUnlimitedExactTenantOnly(t *testing.T) {
	ctx := context.Background()
	for _, list := range []string{"", " , "} {
		got, err := WithOperatorUnlimitedTenants(Policy{}, list).Resolve(ctx, "eval-tb20-20261109")
		if err != nil || got != DefaultLimits() {
			t.Fatalf("empty operator list changed defaults: %+v %v", got, err)
		}
	}
	p := WithOperatorUnlimitedTenants(Policy{}, " eval-tb20-20261109, another-eval ")
	for _, tenant := range []string{"eval-tb20-20261109", "another-eval"} {
		got, err := p.Resolve(ctx, tenant)
		if err != nil || got.Daily.Enabled || got.Monthly.Enabled {
			t.Fatalf("exact operator entry must disable caps: %s %+v %v", tenant, got, err)
		}
	}
	for _, tenant := range []string{"", "eval-tb20", "eval-tb20-20261109-other", "other-eval-tb20-20261109", "EVAL-TB20-20261109", " eval-tb20-20261109"} {
		got, err := p.Resolve(ctx, tenant)
		if err != nil || got != DefaultLimits() {
			t.Fatalf("nonmatch must retain defaults: %q %+v %v", tenant, got, err)
		}
	}
}

func TestOperatorUnlimitedPreservesZeroAndCeilingForOtherTenants(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.json")
	write := func(doc string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(doc), 0600); err != nil {
			t.Fatal(err)
		}
	}
	base := Policy{Path: path}
	p := WithOperatorUnlimitedTenants(base, "eval")
	write(`{"ceiling":{"daily_usd":45,"monthly_usd":45},"tenants":{"other":{"model_budget":{"daily_usd":0}}}}`)
	got, err := p.Resolve(context.Background(), "other")
	if err != nil || !got.Daily.Enabled || got.Daily.USDMicros != 0 {
		t.Fatalf("zero must remain an enforced zero cap: %+v %v", got, err)
	}
	write(`{"ceiling":{"daily_usd":45,"monthly_usd":45},"tenants":{"other":{"model_budget":{"daily_usd":46}}}}`)
	if _, err := p.Resolve(context.Background(), "other"); err == nil {
		t.Fatal("nonmatching tenant must still obey the hard ceiling")
	}
	got, err = p.Resolve(context.Background(), "eval")
	if err != nil || got != (Limits{}) {
		t.Fatalf("operator exemption must be explicit disabled caps, not huge numbers: %+v %v", got, err)
	}
	write(`{"model_budget":{"unknown":1}}`)
	if _, err := p.Resolve(context.Background(), "other"); err == nil {
		t.Fatal("nonmatching tenant policy parsing must remain strict")
	}
}

func TestOperatorUnlimitedDoesNotIgnoreCancelledContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := WithOperatorUnlimitedTenants(Policy{}, "eval").Resolve(ctx, "eval"); err != context.Canceled {
		t.Fatalf("cancelled context: %v", err)
	}
}
