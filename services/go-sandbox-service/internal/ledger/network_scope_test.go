package ledger

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"testing"
	"time"
)

func TestNetworkScopeMigrationPreservesLegacyRows(t *testing.T) {
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Migrate(t.Context(), Migrations[:len(Migrations)-1]); err != nil {
		t.Fatal(err)
	}
	db, err := store.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	clock := testkit.NewFakeClock()
	err = db.Transaction(t.Context(), relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `INSERT INTO sandbox_grants (grant_id,task_id,hosts,ports,expires_at,state,created_at) VALUES ('old','task','["example.com"]','[443]',$1,'active',$2)`, ISO(clock.Now().Add(time.Hour)), ISO(clock.Now()))
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if err = store.Migrate(t.Context(), Migrations); err != nil {
		t.Fatal(err)
	}
	ledger := New(store, clock)
	grants, err := ledger.GrantsForTask(t.Context(), "t1", "task")
	if err != nil || len(grants) != 1 || grants[0].Scope != "hosts" || grants[0].Audited() {
		t.Fatalf("legacy %+v %v", grants, err)
	}
	if !(Grant{Scope: "public"}).Audited() {
		t.Fatal("public scope must use the gateway")
	}
}
