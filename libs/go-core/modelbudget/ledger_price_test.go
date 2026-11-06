package modelbudget_test

import (
	"context"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// R-06-C: each provider attempt freezes the price it was charged at and the
// usage it was settled with, so a historical cost can be recomputed after the
// price table changes. model_usage stays the only record.

var gpt = modelbudget.Price{InPerMMicros: 1_750_000, OutPerMMicros: 14_000_000, CacheReadPerMMicros: 175_000, PriceAt: "2026-10-21"}

func usageRow(t *testing.T, p relational.Provider, tenant, id string) relational.Row {
	t.Helper()
	s, err := p.Bind(&commonv1.TenantContext{TenantId: tenant})
	if err != nil {
		t.Fatal(err)
	}
	var row relational.Row
	err = s.Transaction(context.Background(), relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		r, found, err := tx.QueryOne(ctx, `SELECT actual_usd_micros, price_currency, price_unit, price_at,
			in_usd_micros_per_m, out_usd_micros_per_m, cache_read_usd_micros_per_m, input_tokens, output_tokens, cache_read_tokens,
			registry_scope, registry_revision, registry_document_sha256
			FROM model_usage WHERE reservation_id = $1`, id)
		if err == nil && !found {
			t.Fatalf("reservation %s not visible to %s", id, tenant)
		}
		row = r
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	return row
}

func TestPriceFormulaMatchesProfileCost(t *testing.T) {
	// 600 uncached input, 400 cache read, 200 output at 1.75 / 0.175 / 14 USD per 1M tokens.
	if got := gpt.USDMicros(modelbudget.Usage{InputTokens: 1000, OutputTokens: 200, CacheReadTokens: 400}); got != 3920 {
		t.Fatalf("cost = %d, want 3920", got)
	}
	// Cache reads above the input are clamped, as in the Model Profile formula.
	if got := gpt.USDMicros(modelbudget.Usage{InputTokens: 100, CacheReadTokens: 400}); got != 18 {
		t.Fatalf("clamped cost = %d, want 18 (17.5 rounds half up)", got)
	}
	if got := (modelbudget.Price{}).USDMicros(modelbudget.Usage{InputTokens: 1 << 40}); got != 0 {
		t.Fatalf("free price = %d", got)
	}
}

func TestReservationFreezesPriceAndSettlementRecordsUsage(t *testing.T) {
	ctx := context.Background()
	l, p := sqliteLedger(t, t.TempDir())
	price := gpt
	r, err := l.Reserve(ctx, modelbudget.ReservationRequest{
		TenantID: "t1", At: testTime, USDMicros: 9000, Limits: limits(100_000, 100_000),
		TaskID: "task", NodeID: "node", ModelProfileID: "openai/gpt-x@1", Vendor: "openai", Method: "generate", Price: &price,
	})
	if err != nil {
		t.Fatal(err)
	}
	price.InPerMMicros = 99 // The request is copied; later changes do not reach the stored row.
	row := usageRow(t, p, "t1", r.ID)
	want := map[string]any{
		"price_currency": "USD", "price_unit": "usd_micros_per_1m_tokens", "price_at": "2026-10-21",
		"in_usd_micros_per_m": int64(1_750_000), "out_usd_micros_per_m": int64(14_000_000), "cache_read_usd_micros_per_m": int64(175_000),
		"input_tokens": nil, "output_tokens": nil, "cache_read_tokens": nil, "actual_usd_micros": nil,
	}
	for k, v := range want {
		if row[k] != v {
			t.Errorf("reserved %s = %#v, want %#v", k, row[k], v)
		}
	}

	u := modelbudget.Usage{InputTokens: 1000, OutputTokens: 200, CacheReadTokens: 400}
	actual := gpt.USDMicros(u)
	if err := l.SettleUsage(ctx, r, actual, u); err != nil {
		t.Fatal(err)
	}
	if err := l.SettleUsage(ctx, r, actual, u); err != nil {
		t.Fatalf("identical retry: %v", err)
	}
	if err := l.Settle(ctx, r, actual); err != nil {
		t.Fatalf("Settle with the same amount after SettleUsage: %v", err)
	}
	for _, other := range []modelbudget.Usage{{InputTokens: 1000, OutputTokens: 201, CacheReadTokens: 400}, {}} {
		if err := l.SettleUsage(ctx, r, actual, other); providererr.CategoryOf(err) != providererr.Conflict {
			t.Fatalf("different usage %+v: %v", other, err)
		}
	}
	if err := l.SettleUsage(ctx, r, actual+1, u); providererr.CategoryOf(err) != providererr.Conflict {
		t.Fatalf("different amount: %v", err)
	}

	row = usageRow(t, p, "t1", r.ID)
	if row["input_tokens"] != int64(1000) || row["output_tokens"] != int64(200) || row["cache_read_tokens"] != int64(400) || row["actual_usd_micros"] != int64(3920) {
		t.Fatalf("settled row: %v", row)
	}
	// The stored evidence alone reproduces the charge.
	frozen := modelbudget.Price{
		InPerMMicros: row["in_usd_micros_per_m"].(int64), OutPerMMicros: row["out_usd_micros_per_m"].(int64),
		CacheReadPerMMicros: row["cache_read_usd_micros_per_m"].(int64), PriceAt: row["price_at"].(string),
	}
	again := frozen.USDMicros(modelbudget.Usage{InputTokens: row["input_tokens"].(int64), OutputTokens: row["output_tokens"].(int64), CacheReadTokens: row["cache_read_tokens"].(int64)})
	if again != row["actual_usd_micros"].(int64) {
		t.Fatalf("recomputed %d, settled %v", again, row["actual_usd_micros"])
	}
}

func TestPriceEvidenceBackendParity(t *testing.T) {
	eachBackend(t, func(t *testing.T, open func() (modelbudget.Provider, relational.Provider)) {
		ctx := context.Background()
		l, p := open()
		price := gpt
		r, err := l.Reserve(ctx, modelbudget.ReservationRequest{TenantID: "t1", At: testTime, USDMicros: 9000, Limits: limits(100_000, 100_000), Price: &price})
		if err != nil {
			t.Fatal(err)
		}
		u := modelbudget.Usage{InputTokens: 1000, OutputTokens: 200, CacheReadTokens: 400}
		settler := l.(*modelbudget.Ledger)
		if err := settler.SettleUsage(ctx, r, 3920, u); err != nil {
			t.Fatal(err)
		}
		if err := settler.SettleUsage(ctx, r, 3920, modelbudget.Usage{InputTokens: 1000}); providererr.CategoryOf(err) != providererr.Conflict {
			t.Fatalf("different usage: %v", err)
		}
		row := usageRow(t, p, "t1", r.ID)
		if row["price_at"] != "2026-10-21" || row["cache_read_usd_micros_per_m"] != int64(175_000) || row["cache_read_tokens"] != int64(400) || row["actual_usd_micros"] != int64(3920) {
			t.Fatalf("row: %v", row)
		}
		foreign := r
		foreign.TenantID = "t2"
		if err := settler.SettleUsage(ctx, foreign, 3920, u); err == nil {
			t.Fatal("cross-tenant settlement accepted")
		}
	})
}

func TestSettleUsageAfterPlainSettleIsAConflictOnlyWhenUsageDiffers(t *testing.T) {
	ctx := context.Background()
	l, p := sqliteLedger(t, t.TempDir())
	r := reserve(t, l, "t1", testTime, 50, limits(100, 100))
	settle(t, l, r, 30)
	// The amount matches but the earlier settlement recorded no usage: the
	// evidence would change, so it is not an idempotent retry.
	if err := l.SettleUsage(ctx, r, 30, modelbudget.Usage{InputTokens: 10}); providererr.CategoryOf(err) != providererr.Conflict {
		t.Fatalf("adding usage to a settled row: %v", err)
	}
	if err := l.SettleUsage(ctx, r, 30, modelbudget.Usage{}); err != nil {
		t.Fatalf("zero usage equals no usage recorded: %v", err)
	}
	row := usageRow(t, p, "t1", r.ID)
	for _, k := range []string{"input_tokens", "output_tokens", "cache_read_tokens", "price_at", "in_usd_micros_per_m"} {
		if row[k] != nil {
			t.Errorf("%s = %#v, want NULL", k, row[k])
		}
	}
}

func TestPriceAndUsageValidation(t *testing.T) {
	ctx := context.Background()
	l, _ := sqliteLedger(t, t.TempDir())
	for _, bad := range []modelbudget.Price{
		{InPerMMicros: -1, PriceAt: "2026-10-21"},
		{OutPerMMicros: -1, PriceAt: "2026-10-21"},
		{CacheReadPerMMicros: -1, PriceAt: "2026-10-21"},
		{InPerMMicros: 1},
	} {
		_, err := l.Reserve(ctx, modelbudget.ReservationRequest{TenantID: "t1", At: testTime, USDMicros: 1, Limits: limits(100, 100), Price: &bad})
		if providererr.CategoryOf(err) != providererr.Invalid {
			t.Fatalf("price %+v: %v", bad, err)
		}
	}
	r := reserve(t, l, "t1", testTime, 10, limits(100, 100))
	for _, bad := range []modelbudget.Usage{{InputTokens: -1}, {OutputTokens: -1}, {CacheReadTokens: -1}} {
		if err := l.SettleUsage(ctx, r, 1, bad); providererr.CategoryOf(err) != providererr.Invalid {
			t.Fatalf("usage %+v: %v", bad, err)
		}
	}
	if err := l.Check(ctx, "t1", testTime, limits(10, 10)); !modelbudget.IsTenantBudgetExceeded(err) {
		t.Fatalf("invalid settlement must keep the reservation: %v", err)
	}
}

func TestRowsFromMigration301ReadAsUnknownEvidence(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	p, err := sqlitestore.New(sqlitestore.Options{Dir: dir})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Migrate(ctx, modelbudget.Migrations()[:1]); err != nil {
		t.Fatal(err)
	}
	s, err := p.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	err = s.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `INSERT INTO model_usage
			(reservation_id, reserved_at, reserved_usd_micros, actual_usd_micros, task_id, node_id, model_profile_id, vendor, method)
			VALUES ('old', $1, 40, 25, '', '', 'p@1', 'v', 'generate')`, testTime.UnixMilli())
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Close(); err != nil {
		t.Fatal(err)
	}
	l, p2 := sqliteLedger(t, dir)
	row := usageRow(t, p2, "t1", "old")
	for _, k := range []string{"price_currency", "price_unit", "price_at", "in_usd_micros_per_m", "out_usd_micros_per_m", "cache_read_usd_micros_per_m", "input_tokens", "output_tokens", "cache_read_tokens", "registry_scope", "registry_revision", "registry_document_sha256"} {
		if v, ok := row[k]; !ok || v != nil {
			t.Errorf("old row %s = %#v (present %v), want NULL", k, v, ok)
		}
	}
	if row["actual_usd_micros"] != int64(25) {
		t.Fatalf("old charge lost: %v", row)
	}
	if err := l.Check(ctx, "t1", testTime, limits(25, 25)); !modelbudget.IsTenantBudgetExceeded(err) {
		t.Fatalf("old charge no longer counts: %v", err)
	}
}
