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

// 平台硬上限（ceiling）与默认额度（default）分开：免费租户按默认额度，评测与内部租户可以在硬上限之内调高
// （人工验收第 42、52 题）。没写 ceiling 时硬上限等于默认额度，与原来的行为相同。
func TestPolicyCeilingIsSeparateFromDefault(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policies.json")
	write := func(doc string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(doc), 0600); err != nil {
			t.Fatal(err)
		}
	}
	p := Policy{Path: path}
	ctx := context.Background()
	write(`{"ceiling":{"daily_usd":1000,"monthly_usd":10000},"default":{"model_budget":{"daily_usd":3,"monthly_usd":20}},` +
		`"tenants":{"evals":{"model_budget":{"daily_usd":1000,"monthly_usd":10000}}}}`)
	free, err := p.Resolve(ctx, "someone")
	if err != nil || free != DefaultLimits() {
		t.Fatalf("free tenant keeps the default: %+v %v", free, err)
	}
	evals, err := p.Resolve(ctx, "evals")
	if err != nil || evals.Daily.USDMicros != 1_000_000_000 || evals.Monthly.USDMicros != 10_000_000_000 {
		t.Fatalf("eval tenant raised within the ceiling: %+v %v", evals, err)
	}
	write(`{"ceiling":{"daily_usd":100},"tenants":{"evals":{"model_budget":{"daily_usd":101}}}}`)
	if _, err := p.Resolve(ctx, "evals"); err == nil {
		t.Fatal("a tenant above the ceiling must be refused")
	}
	write(`{"ceiling":{"daily_usd":2},"default":{"model_budget":{"daily_usd":3}}}`)
	if _, err := p.Resolve(ctx, "someone"); err == nil {
		t.Fatal("a default above the ceiling must be refused")
	}
	write(`{"ceiling":{"daily_usd":5,"bogus":1}}`)
	if _, err := p.Resolve(ctx, "someone"); err == nil {
		t.Fatal("unknown keys in the ceiling are refused")
	}
	write(`{"tenants":{"t1":{"model_budget":{"monthly_usd":21}}}}`)
	if _, err := p.Resolve(ctx, "t1"); err == nil {
		t.Fatal("without a ceiling the default is the ceiling")
	}
}

