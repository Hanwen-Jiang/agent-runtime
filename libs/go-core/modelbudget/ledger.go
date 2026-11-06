// Package modelbudget provides the shared model-usage ledger used by the Model
// Gateway and Task admission. The existing business RelationalStore owns its
// persistence and tenant isolation; callers must use the same store.
package modelbudget

import (
	"context"
	"crypto/rand"
	"errors"
	"math"
	"math/bits"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/registryref"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

const TenantBudgetExceededCode = "tenant_budget_exceeded"

// Limit distinguishes an absent limit from an explicit zero allowance.
type Limit struct {
	Enabled   bool
	USDMicros int64
}

type Limits struct {
	Daily   Limit
	Monthly Limit
}

// ReservationRequest records one provider attempt before it starts. Limits
// are resolved by the caller; pending and settled usage come only from this
// ledger. At determines the UTC day and natural month charged by the attempt.
// Price, when given, is frozen on the attempt's row so its charge can be
// recomputed after the price table changes; rows without it read NULL.
type ReservationRequest struct {
	TenantID       string
	At             time.Time
	USDMicros      int64
	Limits         Limits
	TaskID         string
	NodeID         string
	ModelProfileID string
	Vendor         string
	Method         string
	Price          *Price
	Registry       *registryref.Profile
}

// Price is the model profile's rate card at reservation time, in USD micros
// per one million tokens (1.75 USD per 1M tokens is 1_750_000). PriceAt is
// the date the rates were taken (the profile's freshness.price_at).
type Price struct {
	InPerMMicros        int64
	OutPerMMicros       int64
	CacheReadPerMMicros int64
	PriceAt             string
}

const (
	PriceCurrency = "USD"
	PriceUnit     = "usd_micros_per_1m_tokens"
)

// Usage is the provider-reported token usage an attempt was settled with.
// InputTokens includes CacheReadTokens.
type Usage struct {
	InputTokens     int64
	OutputTokens    int64
	CacheReadTokens int64
}

// USDMicros recomputes a charge from frozen evidence with the Model Profile
// formula: uncached input, cache reads (clamped to the input) and output at
// their rates, rounded half up to whole micros. Integer arithmetic only.
func (p Price) USDMicros(u Usage) int64 {
	input := max(u.InputTokens, 0)
	cached := min(max(u.CacheReadTokens, 0), input)
	var hi, lo uint64
	for _, term := range [][2]int64{{input - cached, p.InPerMMicros}, {cached, p.CacheReadPerMMicros}, {max(u.OutputTokens, 0), p.OutPerMMicros}} {
		h, l := bits.Mul64(uint64(term[0]), uint64(max(term[1], 0)))
		var carry uint64
		lo, carry = bits.Add64(lo, l, 0)
		hi += h + carry
	}
	lo, carry := bits.Add64(lo, 500_000, 0)
	hi += carry
	if hi >= 1_000_000 {
		return math.MaxInt64
	}
	q, _ := bits.Div64(hi, lo, 1_000_000)
	return int64(min(q, math.MaxInt64))
}

func (p *Price) validate() error {
	if p == nil {
		return nil
	}
	if p.InPerMMicros < 0 || p.OutPerMMicros < 0 || p.CacheReadPerMMicros < 0 {
		return ledgerInvalid("negative_price", "model price must be non-negative")
	}
	if p.PriceAt == "" {
		return ledgerInvalid("missing_price_date", "model price needs the date it was taken")
	}
	return nil
}

// UsageSettler is implemented by Ledger. Callers that know the provider's
// token usage settle through it so the row keeps the usage next to the price.
type UsageSettler interface {
	SettleUsage(context.Context, Reservation, int64, Usage) error
}

// Reservation is returned only after the reservation commits. Pass it back
// unchanged to Settle. At is stored and returned at UTC millisecond precision.
type Reservation struct {
	ID        string
	TenantID  string
	At        time.Time
	USDMicros int64
}

// Provider is the shared boundary for call reservations and task admission.
// Check rejects an exhausted budget without reserving model usage. Reserve
// atomically includes pending calls, allowing a new reservation to exactly
// fill the limit. Settle records actual provider charges, including charges
// above the estimate, and refunds unused reservations. Identical retries of
// Settle are idempotent; a different second settlement is a conflict.
type Provider interface {
	Reserve(context.Context, ReservationRequest) (Reservation, error)
	Settle(context.Context, Reservation, int64) error
	Check(context.Context, string, time.Time, Limits) error
}

type Ledger struct{ store relational.Provider }

var (
	_ Provider     = (*Ledger)(nil)
	_ UsageSettler = (*Ledger)(nil)
)

// New uses an existing business store. The caller owns its lifecycle and must
// apply Migrations before serving requests.
func New(store relational.Provider) *Ledger { return &Ledger{store: store} }

// Migrations registers the single authoritative model call record and one
// per-tenant lock row. The lock serializes read/check/write across Gateway
// processes on Postgres; SQLite write transactions already serialize writes.
// The storage provider adds tenant scope, so application SQL has no tenant
// column. No aggregate balances or recovery state are maintained separately.
// 302 adds the frozen price and settled usage of each attempt (R-06-C); all
// nullable, so rows written before it read as unknown evidence.
func Migrations() []relational.Migration {
	evidence := []string{
		"price_currency TEXT", "price_unit TEXT", "price_at TEXT",
		"in_usd_micros_per_m BIGINT", "out_usd_micros_per_m BIGINT", "cache_read_usd_micros_per_m BIGINT",
		"input_tokens BIGINT", "output_tokens BIGINT", "cache_read_tokens BIGINT",
	}
	addColumns := make([]string, len(evidence))
	for i, column := range evidence {
		addColumns[i] = "ALTER TABLE model_usage ADD COLUMN " + column
	}
	return []relational.Migration{{
		Version: 301,
		Name:    "model_usage_reservations",
		Statements: []string{
			`CREATE TABLE model_usage (
				reservation_id TEXT PRIMARY KEY,
				reserved_at BIGINT NOT NULL,
				reserved_usd_micros BIGINT NOT NULL,
				actual_usd_micros BIGINT,
				task_id TEXT NOT NULL,
				node_id TEXT NOT NULL,
				model_profile_id TEXT NOT NULL,
				vendor TEXT NOT NULL,
				method TEXT NOT NULL
			)`,
			`CREATE INDEX model_usage_by_time ON model_usage (reserved_at)`,
			`CREATE TABLE model_budget_lock (lock_id BIGINT PRIMARY KEY)`,
		},
	}, {
		Version:    302,
		Name:       "model_usage_price_evidence",
		Statements: addColumns,
	}, {
		Version: 303,
		Name:    "model_usage_registry_evidence",
		Statements: []string{
			"ALTER TABLE model_usage ADD COLUMN registry_scope TEXT",
			"ALTER TABLE model_usage ADD COLUMN registry_revision TEXT",
			"ALTER TABLE model_usage ADD COLUMN registry_document_sha256 TEXT",
		},
	}}
}

// IsTenantBudgetExceeded recognizes the stable non-retryable refusal.
func IsTenantBudgetExceeded(err error) bool {
	var pe *providererr.Error
	return errors.As(err, &pe) && pe.Code == TenantBudgetExceededCode
}

func ledgerInvalid(code, message string) error {
	return providererr.New(providererr.Invalid, code, message)
}

func exceeded() error {
	return providererr.New(providererr.Denied, TenantBudgetExceededCode, "tenant model budget exceeded")
}

func validatePeriod(at time.Time, limits Limits) error {
	if at.IsZero() {
		return ledgerInvalid("missing_reservation_time", "reservation time is required")
	}
	if limits.Daily.USDMicros < 0 || limits.Monthly.USDMicros < 0 {
		return ledgerInvalid("negative_limit", "budget limit must be non-negative")
	}
	return nil
}

func (l *Ledger) transaction(ctx context.Context, tenantID string, readOnly bool, fn func(context.Context, relational.Tx) error) error {
	if err := ctx.Err(); err != nil {
		return providererr.Wrap(providererr.Transient, "ledger_context", "budget ledger operation canceled", err)
	}
	s, err := l.store.Bind(&commonv1.TenantContext{TenantId: tenantID})
	if err != nil {
		return err
	}
	return s.Transaction(ctx, relational.TxOptions{ReadOnly: readOnly}, fn)
}

func lockTenant(ctx context.Context, tx relational.Tx) error {
	if _, err := tx.Execute(ctx, `INSERT INTO model_budget_lock (lock_id) VALUES (1) ON CONFLICT DO NOTHING`); err != nil {
		return err
	}
	_, err := tx.Execute(ctx, `UPDATE model_budget_lock SET lock_id = lock_id WHERE lock_id = 1`)
	return err
}

type periodUsage struct{ daily, monthly int64 }

// usage reads only the current UTC month using the time index. Settled rows
// contribute actual charges; pending rows contribute their reservation.
// Explicit BIGINT casts also normalize Postgres SUM(BIGINT)'s numeric result
// to RelationalStore's portable int64 values.
func usage(ctx context.Context, tx relational.Tx, at time.Time) (periodUsage, error) {
	u := at.UTC()
	day := time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC)
	month := time.Date(u.Year(), u.Month(), 1, 0, 0, 0, 0, time.UTC)
	row, _, err := tx.QueryOne(ctx, `SELECT
		CAST(COALESCE(SUM(CASE WHEN reserved_at >= $1 AND reserved_at < $2
			THEN COALESCE(actual_usd_micros, reserved_usd_micros) ELSE 0 END), 0) AS BIGINT) AS daily_total,
		CAST(COALESCE(SUM(COALESCE(actual_usd_micros, reserved_usd_micros)), 0) AS BIGINT) AS monthly_total
		FROM model_usage WHERE reserved_at >= $3 AND reserved_at < $4`,
		day.UnixMilli(), day.AddDate(0, 0, 1).UnixMilli(), month.UnixMilli(), month.AddDate(0, 1, 0).UnixMilli())
	if err != nil {
		return periodUsage{}, err
	}
	return periodUsage{daily: row["daily_total"].(int64), monthly: row["monthly_total"].(int64)}, nil
}

func canReserve(total, amount int64, limit Limit) bool {
	return !limit.Enabled || (total < limit.USDMicros && amount <= limit.USDMicros-total)
}

// Reserve atomically checks both periods and inserts the pending charge.
// Even a zero-cost attempt is refused once an enabled limit is exhausted.
func (l *Ledger) Reserve(ctx context.Context, req ReservationRequest) (Reservation, error) {
	if err := validatePeriod(req.At, req.Limits); err != nil {
		return Reservation{}, err
	}
	if req.USDMicros < 0 {
		return Reservation{}, ledgerInvalid("negative_reservation", "reservation amount must be non-negative")
	}
	if err := req.Price.validate(); err != nil {
		return Reservation{}, err
	}
	var registryScope, registryRevision, registryDigest any
	if ref := req.Registry; ref != nil {
		if ref.Validate() != nil || req.ModelProfileID == "" {
			return Reservation{}, ledgerInvalid("invalid_registry_reference", "model usage needs a valid published profile reference")
		}
		registryScope, registryRevision, registryDigest = ref.Scope, ref.Version, ref.SHA256
	}
	var currency, unit, priceAt, in, out, cacheRead any
	if p := req.Price; p != nil {
		currency, unit, priceAt, in, out, cacheRead = PriceCurrency, PriceUnit, p.PriceAt, p.InPerMMicros, p.OutPerMMicros, p.CacheReadPerMMicros
	}
	r := Reservation{ID: "mbr_" + rand.Text(), TenantID: req.TenantID, At: req.At.UTC().Truncate(time.Millisecond), USDMicros: req.USDMicros}
	err := l.transaction(ctx, req.TenantID, false, func(ctx context.Context, tx relational.Tx) error {
		if err := lockTenant(ctx, tx); err != nil {
			return err
		}
		u, err := usage(ctx, tx, r.At)
		if err != nil {
			return err
		}
		if !canReserve(u.daily, req.USDMicros, req.Limits.Daily) || !canReserve(u.monthly, req.USDMicros, req.Limits.Monthly) {
			return exceeded()
		}
		if req.USDMicros > math.MaxInt64-u.daily || req.USDMicros > math.MaxInt64-u.monthly {
			return ledgerInvalid("reservation_overflow", "reservation amount is too large")
		}
		_, err = tx.Execute(ctx, `INSERT INTO model_usage
			(reservation_id, reserved_at, reserved_usd_micros, actual_usd_micros, task_id, node_id, model_profile_id, vendor, method,
			 price_currency, price_unit, price_at, in_usd_micros_per_m, out_usd_micros_per_m, cache_read_usd_micros_per_m,
			 registry_scope, registry_revision, registry_document_sha256)
			VALUES ($1, $2, $3, NULL, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)`,
			r.ID, r.At.UnixMilli(), r.USDMicros, req.TaskID, req.NodeID, req.ModelProfileID, req.Vendor, req.Method,
			currency, unit, priceAt, in, out, cacheRead, registryScope, registryRevision, registryDigest)
		return err
	})
	if err != nil {
		return Reservation{}, err
	}
	return r, nil
}

// Check consults the same durable usage as Reserve. It creates no reservation:
// the Gateway still reserves each attempt immediately before provider I/O.
func (l *Ledger) Check(ctx context.Context, tenantID string, at time.Time, limits Limits) error {
	if err := validatePeriod(at, limits); err != nil {
		return err
	}
	return l.transaction(ctx, tenantID, true, func(ctx context.Context, tx relational.Tx) error {
		u, err := usage(ctx, tx, at)
		if err != nil {
			return err
		}
		if !canReserve(u.daily, 0, limits.Daily) || !canReserve(u.monthly, 0, limits.Monthly) {
			return exceeded()
		}
		return nil
	})
}

// Settle replaces a pending estimate with actual usage in its original UTC
// periods. Provider charges cannot be discarded if they exceed the estimate;
// the resulting total blocks subsequent requests. Unknown usage must remain
// pending, not be settled to zero merely because a caller disconnected.
func (l *Ledger) Settle(ctx context.Context, r Reservation, actualUSDMicros int64) error {
	return l.settle(ctx, r, actualUSDMicros, nil)
}

// SettleUsage is Settle that also records the provider's token usage next to
// the frozen price. A retry is idempotent only with the same amount and the
// same usage; a row settled without usage counts as zero tokens.
func (l *Ledger) SettleUsage(ctx context.Context, r Reservation, actualUSDMicros int64, u Usage) error {
	if u.InputTokens < 0 || u.OutputTokens < 0 || u.CacheReadTokens < 0 {
		return ledgerInvalid("negative_usage", "token usage must be non-negative")
	}
	return l.settle(ctx, r, actualUSDMicros, &u)
}

func storedTokens(row relational.Row, column string) int64 {
	if v, ok := row[column].(int64); ok {
		return v
	}
	return 0
}

func (l *Ledger) settle(ctx context.Context, r Reservation, actualUSDMicros int64, u *Usage) error {
	if r.ID == "" || r.At.IsZero() || r.USDMicros < 0 {
		return ledgerInvalid("invalid_reservation", "reservation handle is invalid")
	}
	if actualUSDMicros < 0 {
		return ledgerInvalid("negative_settlement", "actual usage must be non-negative")
	}
	return l.transaction(ctx, r.TenantID, false, func(ctx context.Context, tx relational.Tx) error {
		if err := lockTenant(ctx, tx); err != nil {
			return err
		}
		row, found, err := tx.QueryOne(ctx, `SELECT reserved_at, reserved_usd_micros, actual_usd_micros,
			input_tokens, output_tokens, cache_read_tokens FROM model_usage WHERE reservation_id = $1`, r.ID)
		if err != nil {
			return err
		}
		if !found {
			return ledgerInvalid("unknown_reservation", "reservation is not known")
		}
		if !r.At.Equal(time.UnixMilli(row["reserved_at"].(int64))) || r.USDMicros != row["reserved_usd_micros"].(int64) {
			return ledgerInvalid("reservation_mismatch", "reservation handle does not match")
		}
		if previous := row["actual_usd_micros"]; previous != nil {
			same := previous.(int64) == actualUSDMicros
			if same && u != nil {
				same = *u == Usage{InputTokens: storedTokens(row, "input_tokens"), OutputTokens: storedTokens(row, "output_tokens"), CacheReadTokens: storedTokens(row, "cache_read_tokens")}
			}
			if same {
				return nil
			}
			return providererr.New(providererr.Conflict, "reservation_already_settled", "reservation was settled with different usage")
		}
		if u == nil {
			_, err = tx.Execute(ctx, `UPDATE model_usage SET actual_usd_micros = $1 WHERE reservation_id = $2`, actualUSDMicros, r.ID)
			return err
		}
		_, err = tx.Execute(ctx, `UPDATE model_usage SET actual_usd_micros = $1, input_tokens = $2, output_tokens = $3, cache_read_tokens = $4
			WHERE reservation_id = $5`, actualUSDMicros, u.InputTokens, u.OutputTokens, u.CacheReadTokens, r.ID)
		return err
	})
}
