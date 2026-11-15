package modelbudget_test

import (
	"context"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func TestOperatorUnlimitedStillTracksKnownAndPendingUsage(t *testing.T) {
	ctx := context.Background()
	l, p := sqliteLedger(t, t.TempDir())
	policy, err := modelbudget.WithOperatorUnlimitedTenants(modelbudget.Policy{}, "eval").Resolve(ctx, "eval")
	if err != nil {
		t.Fatal(err)
	}
	// Charges exceed the ordinary daily and monthly defaults but remain recorded.
	r := reserve(t, l, "eval", testTime, 50_000_000, policy)
	settle(t, l, r, 40_000_000)
	reserve(t, l, "eval", testTime, 60_000_000, policy)
	if err := l.Check(ctx, "eval", testTime, policy); err != nil {
		t.Fatalf("disabled caps rejected recorded usage: %v", err)
	}
	db, err := p.Bind(&commonv1.TenantContext{TenantId: "eval"})
	if err != nil {
		t.Fatal(err)
	}
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, "SELECT reserved_usd_micros, actual_usd_micros FROM model_usage ORDER BY reserved_usd_micros")
		if err != nil {
			return err
		}
		if len(rows) != 2 || rows[0]["actual_usd_micros"] != int64(40_000_000) || rows[1]["actual_usd_micros"] != nil || rows[1]["reserved_usd_micros"] != int64(60_000_000) {
			t.Fatalf("known and pending evidence lost: %#v", rows)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
