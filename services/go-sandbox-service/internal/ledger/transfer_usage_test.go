package ledger

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	"path/filepath"
	"testing"
	"time"
)

func TestTransferQuotaUnknownOutcomeRetainedAndSettlementIsIdempotent(t *testing.T) {
	store, e := sqlitestore.New(sqlitestore.Options{Dir: filepath.Join(t.TempDir(), "db")})
	if e != nil {
		t.Fatal(e)
	}
	defer store.Close()
	clock := testkit.NewFakeClock()
	l := New(store, clock)
	ctx := context.Background()
	expiry := clock.Now().Add(2 * time.Minute)
	if e = l.ReserveTransferBytes(ctx, "tenant", "one", "cf", "env", "task", 90, 100, expiry); e != nil {
		t.Fatal(e)
	}
	if e = l.ReserveTransferBytes(ctx, "tenant", "two", "cf", "env", "task", 20, 100, expiry); e == nil {
		t.Fatal("exceeded quota while prior upload unknown")
	}
	clock.Advance(3 * time.Minute)
	rows, e := l.ExpiredTransferUsage(ctx, "tenant")
	if e != nil || len(rows) != 1 || rows[0]["actual_bytes"] != nil {
		t.Fatalf("unknown reservation %v %v", rows, e)
	}
	if e = l.SettleTransferBytes(ctx, "tenant", "one", 30); e != nil {
		t.Fatal(e)
	}
	if e = l.SettleTransferBytes(ctx, "tenant", "one", 30); e != nil {
		t.Fatal(e)
	}
	if e = l.SettleTransferBytes(ctx, "tenant", "one", 20); e == nil {
		t.Fatal("changed terminal accounting")
	}
	if e = l.SettleTransferBytes(ctx, "other", "one", 30); e == nil {
		t.Fatal("cross tenant settlement")
	}
	if e = l.ReserveTransferBytes(ctx, "tenant", "two", "cf", "env", "task", 60, 100, expiry); e != nil {
		t.Fatal(e)
	}
	var used int64
	if e = l.Tx(ctx, "tenant", true, func(ctx context.Context, tx relational.Tx) error {
		r, _, e := tx.QueryOne(ctx, `SELECT egress_bytes FROM sandbox_usage WHERE day=$1`, clock.Now().Format("2006-01-02"))
		used = Int(r, "egress_bytes")
		return e
	}); e != nil || used != 90 {
		t.Fatalf("quota after settlement=%d %v", used, e)
	}
	if e = l.AcknowledgeTransferUsage(ctx, "tenant", "one"); e != nil {
		t.Fatal(e)
	}
	rows, e = l.ExpiredTransferUsage(ctx, "tenant")
	if e != nil || len(rows) != 1 || Str(rows[0], "grant_id") != "two" {
		t.Fatalf("pending %v %v", rows, e)
	}
}
