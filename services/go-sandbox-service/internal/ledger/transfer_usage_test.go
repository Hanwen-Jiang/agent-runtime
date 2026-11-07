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

func TestDestroyedEnvironmentOwnerStillFindsPendingTransferAccounting(t *testing.T) {
	store, err := sqlitestore.New(sqlitestore.Options{Dir: filepath.Join(t.TempDir(), "db")})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	clock := testkit.NewFakeClock()
	l := New(store, clock)
	ctx := context.Background()
	record, err := l.BeginCreate(ctx, "gone-owner", CreateArgs{Backend: "cf", Kind: "cloudflare", Isolation: "microvm", Idle: time.Minute, MaxLifetime: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	record, err = l.MarkReady(ctx, record, "cfs-0123456789abcdef", "", true)
	if err != nil {
		t.Fatal(err)
	}
	if err = l.ReserveTransferBytes(ctx, "gone-owner", "orphan-grant", "cf", record.BackendEnvID, "task", 90, 100, clock.Now().Add(time.Minute)); err != nil {
		t.Fatal(err)
	}
	record, err = l.BeginDestroy(ctx, record, "test teardown")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = l.MarkDestroyed(ctx, record, 0, 0); err != nil {
		t.Fatal(err)
	}
	clock.Advance(2 * time.Minute)
	active, err := l.Directory(ctx, true)
	if err != nil || len(active) != 0 {
		t.Fatalf("active directory: %v %v", active, err)
	}
	all, err := l.Directory(ctx, false)
	if err != nil || len(all) != 1 || all[0].Owner != "gone-owner" || all[0].State != "destroyed" {
		t.Fatalf("retained directory: %v %v", all, err)
	}
	rows, err := l.ExpiredTransferUsage(ctx, all[0].Owner)
	if err != nil || len(rows) != 1 || rows[0]["actual_bytes"] != nil || Int(rows[0], "reserved_bytes") != 90 {
		t.Fatalf("pending accounting disappeared or refunded: %v %v", rows, err)
	}
	if err = l.SettleTransferBytes(ctx, all[0].Owner, "orphan-grant", 40); err != nil {
		t.Fatal(err)
	}
	rows, err = l.ExpiredTransferUsage(ctx, all[0].Owner)
	if err != nil || len(rows) != 1 || Int(rows[0], "actual_bytes") != 40 {
		t.Fatalf("post-destroy settlement: %v %v", rows, err)
	}
}
