package ledger

import (
	"context"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

func TestUnlimitedTaskGrantReadAndRevocation(t *testing.T) {
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	clock := testkit.NewFakeClock()
	l := New(store, clock)
	const unlimited = "9999-12-31T23:59:59.000000Z"
	if got := ISO(ParseISO(unlimited)); got != unlimited {
		t.Fatalf("sentinel roundtrip: %s", got)
	}
	insert := func(id, task, expiry string) {
		t.Helper()
		if err := l.Tx(t.Context(), "t1", false, func(ctx context.Context, tx relational.Tx) error {
			_, err := tx.Execute(ctx, `INSERT INTO sandbox_grants(grant_id,task_id,scope,hosts,ports,expires_at,state,created_at) VALUES($1,$2,'public','[]','[443]',$3,'active',$4)`, id, task, expiry, ISO(clock.Now()))
			return err
		}); err != nil {
			t.Fatal(err)
		}
	}
	insert("finite", "task", ISO(clock.Now().Add(15*time.Minute)))
	insert("unlimited", "task", unlimited)
	clock.Advance(24 * time.Hour)
	grants, err := l.GrantsForTask(t.Context(), "t1", "task")
	if err != nil || len(grants) != 1 || grants[0].GrantID != "unlimited" || !grants[0].Audited() {
		t.Fatalf("active grants: %+v %v", grants, err)
	}
	if other, err := l.GrantsForTask(t.Context(), "t2", "task"); err != nil || len(other) != 0 {
		t.Fatalf("tenant isolation: %+v %v", other, err)
	}
	if other, err := l.GrantsForTask(t.Context(), "t1", "other-task"); err != nil || len(other) != 0 {
		t.Fatalf("Task isolation: %+v %v", other, err)
	}
	if err := l.RevokeTask(t.Context(), "t1", "task"); err != nil {
		t.Fatal(err)
	}
	if active, err := l.GrantsForTask(t.Context(), "t1", "task"); err != nil || len(active) != 0 {
		t.Fatalf("revoked grant active: %+v %v", active, err)
	}
	insert("second", "task2", unlimited)
	if err := l.RevokeAll(t.Context(), "t1"); err != nil {
		t.Fatal(err)
	}
	if active, err := l.GrantsForTask(t.Context(), "t1", "task2"); err != nil || len(active) != 0 {
		t.Fatalf("tenant revoke failed: %+v %v", active, err)
	}
}
