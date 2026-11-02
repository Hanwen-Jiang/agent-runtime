package budget

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

func TestMemoryProviderAtLimitDeniesWithoutRetry(t *testing.T) {
	p := NewMemoryProvider()
	req := ReservationRequest{
		TenantID:  "tenant-a",
		At:        time.Date(2026, 11, 3, 12, 0, 0, 0, time.FixedZone("CST", 8*60*60)),
		USDMicros: 1_000_000,
		Limits:    Limits{Daily: Limit{Enabled: true, USDMicros: 1_000_000}},
	}
	res, err := p.Reserve(context.Background(), req)
	if err != nil {
		t.Fatalf("first reserve: %v", err)
	}
	if err := p.Settle(context.Background(), res, req.USDMicros); err != nil {
		t.Fatalf("settle: %v", err)
	}

	_, err = p.Reserve(context.Background(), req)
	if !IsTenantBudgetExceeded(err) {
		t.Fatalf("second reserve error = %v, want tenant_budget_exceeded", err)
	}
	if providererr.IsRetryable(err) {
		t.Fatal("tenant_budget_exceeded must not be retryable")
	}
}

func TestMemoryProviderReserveIsAtomicUnderConcurrency(t *testing.T) {
	const attempts = 32
	const limit = 10
	p := NewMemoryProvider()
	req := ReservationRequest{
		TenantID:  "tenant-concurrent",
		At:        time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC),
		USDMicros: 1,
		Limits: Limits{
			Daily:   Limit{Enabled: true, USDMicros: limit},
			Monthly: Limit{Enabled: true, USDMicros: limit},
		},
	}

	var wg sync.WaitGroup
	var mu sync.Mutex
	var successes, denied int
	for range attempts {
		wg.Add(1)
		go func() {
			defer wg.Done()
			res, err := p.Reserve(context.Background(), req)
			if err != nil {
				if !IsTenantBudgetExceeded(err) {
					t.Errorf("reserve error = %v, want tenant_budget_exceeded", err)
				}
				mu.Lock()
				denied++
				mu.Unlock()
				return
			}
			mu.Lock()
			successes++
			mu.Unlock()
			if err := p.Settle(context.Background(), res, req.USDMicros); err != nil {
				t.Errorf("settle: %v", err)
			}
		}()
	}
	wg.Wait()
	if successes != limit || denied != attempts-limit {
		t.Fatalf("successes=%d denied=%d, want successes=%d denied=%d", successes, denied, limit, attempts-limit)
	}
}

func TestMemoryProviderSettlementRefundsRemainder(t *testing.T) {
	p := NewMemoryProvider()
	at := time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)
	limits := Limits{Daily: Limit{Enabled: true, USDMicros: 10}}
	res, err := p.Reserve(context.Background(), ReservationRequest{TenantID: "tenant-refund", At: at, USDMicros: 10, Limits: limits})
	if err != nil {
		t.Fatalf("reserve: %v", err)
	}
	if err := p.Settle(context.Background(), res, 4); err != nil {
		t.Fatalf("settle: %v", err)
	}

	// Six micros were refunded, so a new seven-micro reservation must still be
	// denied by the existing four-micro usage plus the requested seven.
	_, err = p.Reserve(context.Background(), ReservationRequest{TenantID: "tenant-refund", At: at, USDMicros: 7, Limits: limits})
	if !IsTenantBudgetExceeded(err) {
		t.Fatalf("over-limit reserve after refund error = %v, want tenant_budget_exceeded", err)
	}
	// Six micros exactly fit after the refund (4 used + 6 requested = 10).
	if _, err := p.Reserve(context.Background(), ReservationRequest{TenantID: "tenant-refund", At: at, USDMicros: 6, Limits: limits}); err != nil {
		t.Fatalf("reserve refunded amount: %v", err)
	}
}

func TestMemoryProviderUsesUTCDayAndMonthBoundaries(t *testing.T) {
	p := NewMemoryProvider()
	limits := Limits{Daily: Limit{Enabled: true, USDMicros: 1}, Monthly: Limit{Enabled: true, USDMicros: 2}}
	// Local time is already the next local day; UTC conversion places it on
	// 2026-11-03 rather than using the local calendar date.
	first, err := p.Reserve(context.Background(), ReservationRequest{
		TenantID: "tenant-boundary", At: time.Date(2026, 11, 4, 0, 30, 0, 0, time.FixedZone("CST", 8*60*60)), USDMicros: 1, Limits: limits,
	})
	if err != nil {
		t.Fatalf("first boundary reserve: %v", err)
	}
	if err := p.Settle(context.Background(), first, 1); err != nil {
		t.Fatalf("first boundary settle: %v", err)
	}

	// The same local date is a different UTC day, so daily capacity is free;
	// it remains in the same UTC month and consumes the second monthly unit.
	secondAt := time.Date(2026, 11, 4, 23, 30, 0, 0, time.FixedZone("CST", 8*60*60))
	second, err := p.Reserve(context.Background(), ReservationRequest{TenantID: "tenant-boundary", At: secondAt, USDMicros: 1, Limits: limits})
	if err != nil {
		t.Fatalf("second boundary reserve: %v", err)
	}
	if err := p.Settle(context.Background(), second, 1); err != nil {
		t.Fatalf("second boundary settle: %v", err)
	}
	if _, err := p.Reserve(context.Background(), ReservationRequest{TenantID: "tenant-boundary", At: time.Date(2026, 11, 5, 12, 0, 0, 0, time.UTC), USDMicros: 1, Limits: limits}); !IsTenantBudgetExceeded(err) {
		t.Fatalf("same-month reserve error = %v, want tenant_budget_exceeded", err)
	}

	// A new UTC month starts a fresh monthly bucket.
	if _, err := p.Reserve(context.Background(), ReservationRequest{TenantID: "tenant-boundary", At: time.Date(2026, 12, 1, 0, 1, 0, 0, time.UTC), USDMicros: 1, Limits: limits}); err != nil {
		t.Fatalf("new-month reserve: %v", err)
	}
}

func TestMemoryProviderRejectsSettlementAboveReservation(t *testing.T) {
	p := NewMemoryProvider()
	res, err := p.Reserve(context.Background(), ReservationRequest{
		TenantID:  "tenant-settlement",
		At:        time.Date(2026, 11, 3, 0, 0, 0, 0, time.UTC),
		USDMicros: 3,
	})
	if err != nil {
		t.Fatalf("reserve: %v", err)
	}
	if err := p.Settle(context.Background(), res, 4); err == nil {
		t.Fatal("settlement above reservation must fail")
	} else {
		var pe *providererr.Error
		if !errors.As(err, &pe) || pe.Code != "settlement_exceeds_reservation" || providererr.IsRetryable(err) {
			t.Fatalf("settlement error = %v, want non-retryable settlement_exceeds_reservation", err)
		}
	}
}
