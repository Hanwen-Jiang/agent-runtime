package modelbudget_test

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

var testTime = time.Date(2026, 11, 3, 12, 0, 0, 123456789, time.UTC)

func sqliteLedger(t *testing.T, dir string) (*modelbudget.Ledger, *sqlitestore.Provider) {
	t.Helper()
	p, err := sqlitestore.New(sqlitestore.Options{Dir: dir})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	if err := p.Migrate(context.Background(), modelbudget.Migrations()); err != nil {
		t.Fatal(err)
	}
	return modelbudget.New(p), p
}

func limits(daily, monthly int64) modelbudget.Limits {
	return modelbudget.Limits{
		Daily:   modelbudget.Limit{Enabled: true, USDMicros: daily},
		Monthly: modelbudget.Limit{Enabled: true, USDMicros: monthly},
	}
}

func reserve(t *testing.T, l modelbudget.Provider, tenant string, at time.Time, amount int64, policy modelbudget.Limits) modelbudget.Reservation {
	t.Helper()
	r, err := l.Reserve(context.Background(), modelbudget.ReservationRequest{TenantID: tenant, At: at, USDMicros: amount, Limits: policy})
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func denied(t *testing.T, err error) {
	t.Helper()
	if !modelbudget.IsTenantBudgetExceeded(err) || providererr.CategoryOf(err) != providererr.Denied || providererr.IsRetryable(err) {
		t.Fatalf("want non-retryable tenant_budget_exceeded, got %v", err)
	}
}

func settle(t *testing.T, l modelbudget.Provider, r modelbudget.Reservation, amount int64) {
	t.Helper()
	if err := l.Settle(context.Background(), r, amount); err != nil {
		t.Fatal(err)
	}
}

func TestSharedLedgerReservationsRefundAndDurability(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	l, p := sqliteLedger(t, dir)
	policy := limits(100, 500)
	r := reserve(t, l, "t1", testTime, 100, policy)
	denied(t, l.Check(ctx, "t1", testTime, policy))
	_, err := l.Reserve(ctx, modelbudget.ReservationRequest{TenantID: "t1", At: testTime, USDMicros: 1, Limits: policy})
	denied(t, err)
	// A new process sees the durable pending amount, including after a crash.
	if err := p.Close(); err != nil {
		t.Fatal(err)
	}
	l2, _ := sqliteLedger(t, dir)
	denied(t, l2.Check(ctx, "t1", testTime, policy))
	settle(t, l2, r, 30)
	settle(t, l2, r, 30) // An acknowledged-or-lost response can be retried safely.
	if err := l2.Check(ctx, "t1", testTime, policy); err != nil {
		t.Fatal(err)
	}
	reserve(t, l2, "t1", testTime, 70, policy)
	denied(t, l2.Check(ctx, "t1", testTime, policy))
	if err := l2.Check(ctx, "t2", testTime, policy); err != nil {
		t.Fatalf("other tenant's usage leaked: %v", err)
	}
}

func TestLedgerConcurrentProvidersDoNotOversubscribe(t *testing.T) {
	dir := t.TempDir()
	l1, _ := sqliteLedger(t, dir)
	// Create the scope before the independent provider opens it; this tests
	// reservation locking, not concurrent schema initialization.
	if err := l1.Check(context.Background(), "t1", testTime, limits(10, 10)); err != nil {
		t.Fatal(err)
	}
	l2, _ := sqliteLedger(t, dir)
	testConcurrentReservations(t, []modelbudget.Provider{l1, l2})
}

func testConcurrentReservations(t *testing.T, providers []modelbudget.Provider) {
	t.Helper()
	var wg sync.WaitGroup
	results := make(chan error, 32)
	start := make(chan struct{})
	for i := range 32 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			_, err := providers[i%len(providers)].Reserve(context.Background(), modelbudget.ReservationRequest{
				TenantID: "t1", At: testTime, USDMicros: 1, Limits: limits(10, 10),
			})
			results <- err
		}()
	}
	close(start)
	wg.Wait()
	close(results)
	accepted := 0
	for err := range results {
		if err == nil {
			accepted++
		} else {
			denied(t, err)
		}
	}
	if accepted != 10 {
		t.Fatalf("accepted %d reservations, want exactly 10", accepted)
	}
}

func TestLedgerUTCPeriodsAndSettlementKeepsOriginalPeriod(t *testing.T) {
	l, _ := sqliteLedger(t, t.TempDir())
	ctx := context.Background()
	policy := limits(10, 15)
	before := time.Date(2026, 11, 1, 7, 59, 59, 0, time.FixedZone("UTC+8", 8*60*60)) // Oct 31 UTC
	r := reserve(t, l, "t1", before, 10, policy)
	denied(t, l.Check(ctx, "t1", before, policy))
	nextMonth := before.Add(time.Second)
	if err := l.Check(ctx, "t1", nextMonth, policy); err != nil {
		t.Fatal(err)
	}
	settle(t, l, r, 8) // Settlement time does not move October's charge to November.
	r2 := reserve(t, l, "t1", nextMonth, 10, policy)
	settle(t, l, r2, 10)
	nextDay := nextMonth.Add(24 * time.Hour)
	r3 := reserve(t, l, "t1", nextDay, 5, policy)
	denied(t, l.Check(ctx, "t1", nextDay, policy)) // Monthly, not daily, is exhausted.
	settle(t, l, r3, 4)
	reserve(t, l, "t1", nextDay, 1, policy)
	denied(t, l.Check(ctx, "t1", nextDay, policy))
	if err := l.Check(ctx, "t1", time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC), policy); err != nil {
		t.Fatal(err)
	}
}

func TestLedgerActualUsageAboveEstimateStillCharged(t *testing.T) {
	l, _ := sqliteLedger(t, t.TempDir())
	policy := limits(100, 500)
	r := reserve(t, l, "t1", testTime, 25, policy)
	settle(t, l, r, 125)
	denied(t, l.Check(context.Background(), "t1", testTime, policy))
	settle(t, l, r, 125)
	err := l.Settle(context.Background(), r, 25)
	if providererr.CategoryOf(err) != providererr.Conflict {
		t.Fatalf("different repeat settlement = %v, want conflict", err)
	}
}

func TestLedgerZeroAndUnlimitedLimits(t *testing.T) {
	l, _ := sqliteLedger(t, t.TempDir())
	ctx := context.Background()
	for _, policy := range []modelbudget.Limits{
		{Daily: modelbudget.Limit{Enabled: true}},
		{Monthly: modelbudget.Limit{Enabled: true}},
	} {
		denied(t, l.Check(ctx, "t1", testTime, policy))
		_, err := l.Reserve(ctx, modelbudget.ReservationRequest{TenantID: "t1", At: testTime, Limits: policy})
		denied(t, err)
	}
	reserve(t, l, "t1", testTime, 100_000_000, modelbudget.Limits{})
	if err := l.Check(ctx, "t1", testTime, modelbudget.Limits{}); err != nil {
		t.Fatal(err)
	}
	// A zero-cost call is legal only while an enabled budget still has room.
	reserve(t, l, "t2", testTime, 0, limits(1, 1))
	reserve(t, l, "t2", testTime, 1, limits(1, 1))
	_, err := l.Reserve(ctx, modelbudget.ReservationRequest{TenantID: "t2", At: testTime, Limits: limits(1, 1)})
	denied(t, err)
}

func TestLedgerRejectsForgedAndForeignHandles(t *testing.T) {
	l, _ := sqliteLedger(t, t.TempDir())
	r := reserve(t, l, "t1", testTime, 50, limits(100, 100))
	for name, mutate := range map[string]func(*modelbudget.Reservation){
		"tenant": func(r *modelbudget.Reservation) { r.TenantID = "t2" },
		"id":     func(r *modelbudget.Reservation) { r.ID += "forged" },
		"time":   func(r *modelbudget.Reservation) { r.At = r.At.Add(time.Nanosecond) },
		"amount": func(r *modelbudget.Reservation) { r.USDMicros++ },
	} {
		t.Run(name, func(t *testing.T) {
			changed := r
			mutate(&changed)
			if err := l.Settle(context.Background(), changed, 0); err == nil {
				t.Fatal("forged settlement accepted")
			}
		})
	}
	settle(t, l, r, 50)
	reserve(t, l, "t1", testTime, 50, limits(100, 100))
	denied(t, l.Check(context.Background(), "t1", testTime, limits(100, 100)))
}

func TestLedgerPersistsAttribution(t *testing.T) {
	l, p := sqliteLedger(t, t.TempDir())
	ctx := context.Background()
	r, err := l.Reserve(ctx, modelbudget.ReservationRequest{
		TenantID: "t1", At: testTime, USDMicros: 100,
		TaskID: "tsk_1", NodeID: "node_1", ModelProfileID: "vendor/model@version", Vendor: "vendor", Method: "Generate",
	})
	if err != nil {
		t.Fatal(err)
	}
	settle(t, l, r, 37)
	s, err := p.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	err = s.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, found, err := tx.QueryOne(ctx, `SELECT task_id, node_id, model_profile_id, vendor, method, actual_usd_micros FROM model_usage WHERE reservation_id = $1`, r.ID)
		if err != nil {
			return err
		}
		if !found || row["task_id"] != "tsk_1" || row["node_id"] != "node_1" || row["model_profile_id"] != "vendor/model@version" || row["vendor"] != "vendor" || row["method"] != "Generate" || row["actual_usd_micros"] != int64(37) {
			t.Fatalf("wrong ledger row: %+v", row)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestLedgerValidationAndCancelledContext(t *testing.T) {
	l, _ := sqliteLedger(t, t.TempDir())
	valid := modelbudget.ReservationRequest{TenantID: "t1", At: testTime, USDMicros: 10, Limits: limits(100, 100)}
	for _, mutate := range []func(*modelbudget.ReservationRequest){
		func(r *modelbudget.ReservationRequest) { r.TenantID = "" },
		func(r *modelbudget.ReservationRequest) { r.At = time.Time{} },
		func(r *modelbudget.ReservationRequest) { r.USDMicros = -1 },
		func(r *modelbudget.ReservationRequest) { r.Limits.Daily.USDMicros = -1 },
		func(r *modelbudget.ReservationRequest) { r.Limits.Monthly.USDMicros = -1 },
	} {
		r := valid
		mutate(&r)
		if _, err := l.Reserve(context.Background(), r); err == nil {
			t.Fatalf("invalid reservation accepted: %+v", r)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := l.Reserve(ctx, valid)
	if !errors.Is(err, context.Canceled) || !providererr.IsRetryable(err) {
		t.Fatalf("canceled request = %v, want transient context cancellation", err)
	}
	r := reserve(t, l, "t1", testTime, 10, valid.Limits)
	if err := l.Settle(context.Background(), r, -1); err == nil {
		t.Fatal("negative settlement accepted")
	}
	settle(t, l, r, 0)
}
