// Package budget defines the narrow contract the Model Gateway needs from the
// authoritative tenant usage ledger.
//
// This package deliberately does not implement persistence or wire the
// provider into the Gateway.  MemoryProvider is a deterministic contract fake
// for tests only.  A production provider must use the same authoritative usage
// record as task admission; adding a second database here would allow the two
// checks to disagree.
package budget

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

// TenantBudgetExceededCode is returned when a new reservation would exceed a
// configured daily or monthly tenant limit.  It is a denied error and is
// therefore not retryable by the shared provider error policy.
const TenantBudgetExceededCode = "tenant_budget_exceeded"

// Limit is one optional period limit. Enabled=false means that this period is
// not constrained. Enabled=true with USDMicros=0 is a valid zero budget.
type Limit struct {
	Enabled   bool
	USDMicros int64
}

// Limits are the daily and natural-month limits evaluated at reservation time.
// The caller supplies the policy snapshot; the ledger remains the authority
// for usage and pending reservations.
type Limits struct {
	Daily   Limit
	Monthly Limit
}

// ReservationRequest is the cost upper bound to reserve before a model call.
// At is interpreted in UTC for both period boundaries.
type ReservationRequest struct {
	TenantID       string
	At             time.Time
	USDMicros      int64
	Limits         Limits
}

// Reservation is the opaque handle returned by Reserve. Callers must pass
// this handle unchanged to Settle; the provider validates it and rejects
// duplicate or forged handles.
type Reservation struct {
	ID       string
	TenantID string
	At       time.Time
	USDMicros int64
}

// ReservationProvider is the minimum Gateway/Task-admission contract. Reserve
// must atomically account for existing usage and pending reservations in both
// periods. Settle replaces the pending amount with actual usage and refunds
// the remainder. Actual usage must be no greater than the reserved upper bound;
// an upper-bound violation is a provider contract error and must not silently
// overrun the tenant limit.
type ReservationProvider interface {
	Reserve(context.Context, ReservationRequest) (Reservation, error)
	Settle(context.Context, Reservation, int64) error
}

// IsTenantBudgetExceeded reports whether err is the stable denied budget code.
func IsTenantBudgetExceeded(err error) bool {
	var pe *providererr.Error
	return errors.As(err, &pe) && pe.Code == TenantBudgetExceededCode
}

func budgetExceeded() error {
	return providererr.New(providererr.Denied, TenantBudgetExceededCode, "tenant model budget exceeded")
}

func invalid(code, message string) error {
	return providererr.New(providererr.Invalid, code, message)
}

// MemoryProvider is a concurrency-safe in-memory fake for the
// ReservationProvider contract. It is intentionally not a production ledger:
// state is process-local and is lost on restart.
type MemoryProvider struct {
	mu            sync.Mutex
	nextID        uint64
	dailyUsage    map[dailyKey]*periodUsage
	monthlyUsage  map[monthlyKey]*periodUsage
	reservations  map[string]memoryReservation
}

var _ ReservationProvider = (*MemoryProvider)(nil)

type dailyKey struct {
	tenant string
	day    string
}

type monthlyKey struct {
	tenant string
	month  string
}

type periodUsage struct {
	used     int64
	reserved int64
}

type memoryReservation struct {
	reservation Reservation
	dailyKey    dailyKey
	monthlyKey  monthlyKey
	settled     bool
}

// NewMemoryProvider creates an empty contract fake.
func NewMemoryProvider() *MemoryProvider {
	return &MemoryProvider{
		dailyUsage:   make(map[dailyKey]*periodUsage),
		monthlyUsage: make(map[monthlyKey]*periodUsage),
		reservations: make(map[string]memoryReservation),
	}
}

// Reserve atomically checks both configured periods and adds the requested
// amount to their pending reservations. A failed check mutates no state.
func (p *MemoryProvider) Reserve(ctx context.Context, req ReservationRequest) (Reservation, error) {
	if err := contextError(ctx); err != nil {
		return Reservation{}, err
	}
	if req.TenantID == "" {
		return Reservation{}, invalid("missing_tenant", "tenant id is required")
	}
	if req.At.IsZero() {
		return Reservation{}, invalid("missing_reservation_time", "reservation time is required")
	}
	if req.USDMicros < 0 {
		return Reservation{}, invalid("negative_reservation", "reservation amount must be non-negative")
	}
	if err := validateLimits(req.Limits); err != nil {
		return Reservation{}, err
	}

	dailyKey, monthlyKey := makePeriodKeys(req.TenantID, req.At)
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.dailyUsage == nil {
		p.dailyUsage = make(map[dailyKey]*periodUsage)
	}
	if p.monthlyUsage == nil {
		p.monthlyUsage = make(map[monthlyKey]*periodUsage)
	}
	if p.reservations == nil {
		p.reservations = make(map[string]memoryReservation)
	}

	daily := p.dailyUsage[dailyKey]
	if daily == nil {
		daily = &periodUsage{}
	}
	monthly := p.monthlyUsage[monthlyKey]
	if monthly == nil {
		monthly = &periodUsage{}
	}
	if !withinLimit(daily.used, daily.reserved, req.USDMicros, req.Limits.Daily) ||
		!withinLimit(monthly.used, monthly.reserved, req.USDMicros, req.Limits.Monthly) {
		return Reservation{}, budgetExceeded()
	}

	// The checks above are intentionally performed before mutation so a failed
	// daily/monthly check is all-or-none. Check the pending counters again
	// before mutating so an unlimited period cannot overflow int64.
	if !safeAdd(daily.reserved, req.USDMicros) || !safeAdd(monthly.reserved, req.USDMicros) {
		return Reservation{}, invalid("reservation_overflow", "reservation amount is too large")
	}
	daily.reserved += req.USDMicros
	monthly.reserved += req.USDMicros
	p.dailyUsage[dailyKey] = daily
	p.monthlyUsage[monthlyKey] = monthly
	p.nextID++
	id := fmt.Sprintf("mem-res-%d", p.nextID)
	reservation := Reservation{ID: id, TenantID: req.TenantID, At: req.At.UTC(), USDMicros: req.USDMicros}
	p.reservations[id] = memoryReservation{reservation: reservation, dailyKey: dailyKey, monthlyKey: monthlyKey}
	return reservation, nil
}

// Settle replaces the pending reservation with actual usage. The difference
// between the upper bound and actual usage is returned to the same periods.
// A reservation is settled at most once.
func (p *MemoryProvider) Settle(ctx context.Context, reservation Reservation, actualUSDMicros int64) error {
	if err := contextError(ctx); err != nil {
		return err
	}
	if reservation.ID == "" {
		return invalid("missing_reservation", "reservation id is required")
	}
	if actualUSDMicros < 0 {
		return invalid("negative_settlement", "actual usage must be non-negative")
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	record, ok := p.reservations[reservation.ID]
	if !ok {
		return invalid("unknown_reservation", "reservation is not known")
	}
	if record.settled {
		return invalid("reservation_already_settled", "reservation was already settled")
	}
	if record.reservation != reservation {
		return invalid("reservation_mismatch", "reservation handle does not match")
	}
	if actualUSDMicros > reservation.USDMicros {
		return invalid("settlement_exceeds_reservation", "actual usage exceeds reserved amount")
	}

	daily := p.dailyUsage[record.dailyKey]
	monthly := p.monthlyUsage[record.monthlyKey]
	if daily == nil || monthly == nil || daily.reserved < reservation.USDMicros || monthly.reserved < reservation.USDMicros {
		// This can only happen if the fake itself is corrupted. Keep the error
		// explicit rather than silently creating a negative pending balance.
		return invalid("reservation_state_corrupt", "reservation state is inconsistent")
	}
	if !safeAdd(daily.used, actualUSDMicros) || !safeAdd(monthly.used, actualUSDMicros) {
		return invalid("usage_overflow", "actual usage is too large")
	}
	daily.reserved -= reservation.USDMicros
	monthly.reserved -= reservation.USDMicros
	daily.used += actualUSDMicros
	monthly.used += actualUSDMicros
	record.settled = true
	p.reservations[reservation.ID] = record
	return nil
}

func contextError(ctx context.Context) error {
	if ctx == nil {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return providererr.Wrap(providererr.Transient, "ledger_context", "budget ledger operation canceled", err)
	}
	return nil
}

func validateLimits(limits Limits) error {
	for _, limit := range []Limit{limits.Daily, limits.Monthly} {
		if limit.USDMicros < 0 {
			return invalid("negative_limit", "budget limit must be non-negative")
		}
	}
	return nil
}

func makePeriodKeys(tenantID string, at time.Time) (dailyKey, monthlyKey) {
	u := at.UTC()
	return dailyKey{tenant: tenantID, day: u.Format("2006-01-02")}, monthlyKey{tenant: tenantID, month: u.Format("2006-01")}
}

func withinLimit(used, reserved, amount int64, limit Limit) bool {
	if !limit.Enabled {
		return true
	}
	if !safeAdd(used, reserved) {
		return false
	}
	total := used + reserved
	if !safeAdd(total, amount) {
		return false
	}
	return total+amount <= limit.USDMicros
}

func safeAdd(a, b int64) bool {
	const maxInt64 = int64(1<<63 - 1)
	return a >= 0 && b >= 0 && a <= maxInt64-b
}
