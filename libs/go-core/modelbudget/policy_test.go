package modelbudget

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func TestPolicyDefaultOverridesAndHardCeiling(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policies.json")
	p := Policy{Path: path}
	ctx := context.Background()
	got, err := p.Resolve(ctx, "t1")
	if err != nil || got != DefaultLimits() {
		t.Fatalf("default: %+v %v", got, err)
	}
	write := func(doc string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(doc), 0600); err != nil {
			t.Fatal(err)
		}
	}
	write(`{"default":{"model_budget":{"daily_usd":8,"monthly_usd":50}},"tenants":{"t1":{"model_budget":{"daily_usd":0}}}}`)
	got, err = p.Resolve(ctx, "t1")
	if err != nil || got.Daily != (Limit{Enabled: true}) || got.Monthly.USDMicros != 50_000_000 {
		t.Fatalf("zero and inheritance: %+v %v", got, err)
	}
	p.Source = func(context.Context, *commonv1.TenantContext) ([]byte, bool, error) {
		return []byte(`{"model_budget":{"daily_usd":4,"monthly_usd":40}}`), true, nil
	}
	got, err = p.Resolve(ctx, "t2")
	if err != nil || got.Daily.USDMicros != 4_000_000 || got.Monthly.USDMicros != 40_000_000 {
		t.Fatalf("registry: %+v %v", got, err)
	}
	write(`{"tenants":{"t1":{"model_budget":{"monthly_usd":21}}}}`)
	if _, err := p.Resolve(ctx, "t1"); err == nil {
		t.Fatal("tenant exceeded platform ceiling")
	}
}

func TestPolicyRejectsMisspelledOrInvalidBudget(t *testing.T) {
	for _, doc := range []string{
		`{"model_budget":{"daily_us":2}}`,
		`{"model_budjet":{"daily_usd":2}}`,
		`{"model_budget":{"daily_usd":-1}}`,
		`{"model_budget":{"daily_usd":1e20}}`,
		`{"model_budget":{"daily_usd":"3"}}`,
		`{} {}`,
	} {
		p := Policy{Source: func(context.Context, *commonv1.TenantContext) ([]byte, bool, error) { return []byte(doc), true, nil }}
		if _, err := p.Resolve(context.Background(), "t1"); err == nil {
			t.Errorf("accepted %s", doc)
		}
	}
}
