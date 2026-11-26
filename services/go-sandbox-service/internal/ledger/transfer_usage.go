package ledger

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"math"
	"slices"
	"time"
)

type TransferReservation struct {
	Host    string
	Size    int64
	Expires time.Time
}

var TransferUsageMigrations = []relational.Migration{{Version: 217, Name: "sandbox_transfer_usage", Statements: []string{
	`CREATE TABLE sandbox_transfer_usage (grant_id TEXT PRIMARY KEY, backend TEXT NOT NULL, env_id TEXT NOT NULL, task_id TEXT NOT NULL, day TEXT NOT NULL, reserved_bytes BIGINT NOT NULL, actual_bytes BIGINT, expires_at TEXT NOT NULL, acknowledged BIGINT NOT NULL DEFAULT 0)`,
}}}

// ReserveTransferBytes charges the conservative retry budget first. Unknown
// outcomes remain charged until an owner-bound remote accounting receipt arrives.
func (l *Ledger) ReserveTransferBytes(ctx context.Context, owner, id, backend, env, task string, size, limit int64, expires time.Time) error {
	if size < 0 || size > 3*(64<<20) {
		return providererr.New(providererr.Invalid, "transfer_size", "invalid transfer reservation")
	}
	day := l.Now().Format("2006-01-02")
	return l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		if _, e := tx.Execute(ctx, `INSERT INTO sandbox_usage(day,sandbox_seconds,egress_bytes,mirror_miss_bytes,cost_usd) VALUES($1,0,0,0,0) ON CONFLICT(day) DO NOTHING`, day); e != nil {
			return e
		}
		n, e := tx.Execute(ctx, `UPDATE sandbox_usage SET egress_bytes=egress_bytes+$1 WHERE day=$2 AND ($3=0 OR egress_bytes<=$3-$1)`, size, day, limit)
		if e != nil {
			return e
		}
		if n != 1 {
			return providererr.New(providererr.Denied, "sandbox_egress_quota", "transfer exceeds daily network quota")
		}
		_, e = tx.Execute(ctx, `INSERT INTO sandbox_transfer_usage(grant_id,backend,env_id,task_id,day,reserved_bytes,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7)`, id, backend, env, task, day, size, ISO(expires))
		return e
	})
}
func (l *Ledger) SettleTransferBytes(ctx context.Context, owner, id string, actual int64) error {
	return l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		// Lock before reading; duplicate reconciliation cannot refund twice.
		if _, e := tx.Execute(ctx, `UPDATE sandbox_transfer_usage SET reserved_bytes=reserved_bytes WHERE grant_id=$1`, id); e != nil {
			return e
		}
		row, ok, e := tx.QueryOne(ctx, `SELECT day,reserved_bytes,actual_bytes FROM sandbox_transfer_usage WHERE grant_id=$1`, id)
		if e != nil {
			return e
		}
		if !ok {
			return providererr.New(providererr.NotFound, "transfer_usage_missing", "transfer reservation missing")
		}
		if actual < 0 || actual > Int(row, "reserved_bytes") {
			return providererr.New(providererr.Invalid, "transfer_usage_invalid", "transfer count exceeds reservation")
		}
		if row["actual_bytes"] != nil {
			if Int(row, "actual_bytes") != actual {
				return providererr.New(providererr.Conflict, "transfer_usage_changed", "transfer final count changed")
			}
			return nil
		}
		if _, e = tx.Execute(ctx, `UPDATE sandbox_usage SET egress_bytes=egress_bytes-$1 WHERE day=$2`, Int(row, "reserved_bytes")-actual, Str(row, "day")); e != nil {
			return e
		}
		_, e = tx.Execute(ctx, `UPDATE sandbox_transfer_usage SET actual_bytes=$1 WHERE grant_id=$2`, actual, id)
		return e
	})
}
func (l *Ledger) ExpiredTransferUsage(ctx context.Context, owner string) ([]relational.Row, error) {
	var rows []relational.Row
	e := l.Tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		var e error
		rows, e = tx.Query(ctx, `SELECT * FROM sandbox_transfer_usage WHERE acknowledged=0 AND expires_at<=$1`, ISO(l.Now()))
		return e
	})
	return rows, e
}
func (l *Ledger) AcknowledgeTransferUsage(ctx context.Context, owner, id string) error {
	return l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, e := tx.Execute(ctx, `UPDATE sandbox_transfer_usage SET acknowledged=1 WHERE grant_id=$1 AND actual_bytes IS NOT NULL`, id)
		return e
	})
}

func (l *Ledger) ReserveTransferBatch(ctx context.Context, owner, backend, env, task string, limit int64, items []TransferReservation) ([]string, error) {
	if len(items) == 0 {
		return nil, nil
	}
	var totalSize int64
	ids := make([]string, len(items))
	hosts := make([]string, len(items))
	for i, item := range items {
		if item.Size < 0 || item.Size > 3*(64<<20) {
			return nil, providererr.New(providererr.Invalid, "transfer_size", "invalid transfer reservation")
		}
		if totalSize > math.MaxInt64-item.Size {
			return nil, providererr.New(providererr.Invalid, "transfer_size", "invalid transfer reservation")
		}
		totalSize += item.Size
		raw := make([]byte, 16)
		if _, err := rand.Read(raw); err != nil {
			return nil, err
		}
		ids[i] = "sbg-" + hex.EncodeToString(raw)
		encoded, _ := json.Marshal([]string{item.Host})
		hosts[i] = string(encoded)
	}
	now := l.Now()
	day := now.Format("2006-01-02")
	nowISO := ISO(now)
	grantExpires := ISO(now.Add(5 * time.Minute))
	err := l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		if _, e := tx.Execute(ctx, `INSERT INTO sandbox_usage(day,sandbox_seconds,egress_bytes,mirror_miss_bytes,cost_usd) VALUES($1,0,0,0,0) ON CONFLICT(day) DO NOTHING`, day); e != nil {
			return e
		}
		n, e := tx.Execute(ctx, `UPDATE sandbox_usage SET egress_bytes=egress_bytes+$1 WHERE day=$2 AND ($3=0 OR egress_bytes<=$3-$1)`, totalSize, day, limit)
		if e != nil {
			return e
		}
		if n != 1 {
			return providererr.New(providererr.Denied, "sandbox_egress_quota", "transfer exceeds daily network quota")
		}
		for i, item := range items {
			if _, e = tx.Execute(ctx, `INSERT INTO sandbox_grants (grant_id, task_id, hosts, ports, expires_at, state, approved_by, reason, created_at) VALUES ($1,$2,$3,$4,$5,'active','platform:object-transfer','single object transfer',$6)`, ids[i], task, hosts[i], "[443]", grantExpires, nowISO); e != nil {
				return e
			}
			if _, e = tx.Execute(ctx, `INSERT INTO sandbox_transfer_usage(grant_id,backend,env_id,task_id,day,reserved_bytes,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7)`, ids[i], backend, env, task, day, item.Size, ISO(item.Expires)); e != nil {
				return e
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return ids, nil
}

func (l *Ledger) SettleAndRevokeTransferBatch(ctx context.Context, owner string, settled map[string]int64) error {
	if len(settled) == 0 {
		return nil
	}
	ids := make([]string, 0, len(settled))
	for id := range settled {
		ids = append(ids, id)
	}
	slices.Sort(ids)
	nowISO := ISO(l.Now())
	return l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		refundByDay := map[string]int64{}
		for _, id := range ids {
			actual := settled[id]
			if _, e := tx.Execute(ctx, `UPDATE sandbox_transfer_usage SET reserved_bytes=reserved_bytes WHERE grant_id=$1`, id); e != nil {
				return e
			}
			row, ok, e := tx.QueryOne(ctx, `SELECT day,reserved_bytes,actual_bytes FROM sandbox_transfer_usage WHERE grant_id=$1`, id)
			if e != nil {
				return e
			}
			if !ok {
				return providererr.New(providererr.NotFound, "transfer_usage_missing", "transfer reservation missing")
			}
			reserved := Int(row, "reserved_bytes")
			if actual < 0 || actual > reserved {
				return providererr.New(providererr.Invalid, "transfer_usage_invalid", "transfer count exceeds reservation")
			}
			if row["actual_bytes"] != nil {
				if Int(row, "actual_bytes") != actual {
					return providererr.New(providererr.Conflict, "transfer_usage_changed", "transfer final count changed")
				}
			} else {
				refundByDay[Str(row, "day")] += reserved - actual
				if _, e = tx.Execute(ctx, `UPDATE sandbox_transfer_usage SET actual_bytes=$1 WHERE grant_id=$2`, actual, id); e != nil {
					return e
				}
			}
			if _, e = tx.Execute(ctx, `UPDATE sandbox_grants SET state='revoked',revoked_at=$1 WHERE grant_id=$2 AND approved_by='platform:object-transfer'`, nowISO, id); e != nil {
				return e
			}
		}
		days := make([]string, 0, len(refundByDay))
		for d := range refundByDay {
			days = append(days, d)
		}
		slices.Sort(days)
		for _, d := range days {
			if refund := refundByDay[d]; refund != 0 {
				if _, e := tx.Execute(ctx, `UPDATE sandbox_usage SET egress_bytes=egress_bytes-$1 WHERE day=$2`, refund, d); e != nil {
					return e
				}
			}
		}
		return nil
	})
}

func (l *Ledger) AcknowledgeTransferUsageBatch(ctx context.Context, owner string, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	return l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		for _, id := range ids {
			if _, e := tx.Execute(ctx, `UPDATE sandbox_transfer_usage SET acknowledged=1 WHERE grant_id=$1 AND actual_bytes IS NOT NULL`, id); e != nil {
				return e
			}
		}
		return nil
	})
}
