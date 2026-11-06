package mcp

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"testing"
)

func TestPersonalSelectionOnlyLeasesAuthorizedEnabledConnections(t *testing.T) {
	ctx := context.Background()
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err = store.Migrate(ctx, Migrations()); err != nil {
		t.Fatal(err)
	}
	tenant := &commonv1.TenantContext{TenantId: "tenant", UserId: "alice"}
	db, _ := store.Bind(tenant)
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		for _, entry := range []struct {
			id, user string
			enabled  int
		}{{"one", "alice", 1}, {"two", "alice", 1}, {"off", "alice", 0}, {"foreign", "bob", 1}} {
			_, e := tx.Execute(ctx, `INSERT INTO mcp_connections VALUES ($1,$2,$1,'https://mcp.example.com','','','ready','','','','','')`, entry.id, entry.user)
			if e != nil {
				return e
			}
			_, e = tx.Execute(ctx, `INSERT INTO mcp_tools VALUES ($1,'read',$2,'tool','{}','digest','{}',$3,'read',30,'','')`, entry.id, "mcp."+entry.id+".read", entry.enabled)
			if e != nil {
				return e
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	srv, err := New(Deps{Store: store, Ingress: localingress.New(localingress.Options{SigningKey: []byte("01234567890123456789012345678901")}), Policies: func(context.Context, *commonv1.TenantContext) ([]byte, bool, error) {
		return []byte(`{"mcp":{"allowed":true,"hosts":["mcp.example.com"],"max_effect":"read"}}`), true, nil
	}})
	if err != nil {
		t.Fatal(err)
	}
	if err = srv.IssueSelectedLeases(ctx, tenant, "picked", "alice", []string{"two"}); err != nil {
		t.Fatal(err)
	}
	if err = srv.IssueSelectedLeases(ctx, tenant, "empty", "alice", []string{}); err != nil {
		t.Fatal(err)
	}
	if err = srv.IssueLeases(ctx, tenant, "legacy", "alice"); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"off", "foreign", "missing"} {
		if err = srv.IssueSelectedLeases(ctx, tenant, "bad-"+id, "alice", []string{"one", id}); err == nil {
			t.Fatalf("accepted unavailable %s", id)
		}
	}
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		rows, e := tx.Query(ctx, `SELECT task_id,connection_id FROM mcp_leases ORDER BY task_id,connection_id`)
		if e != nil {
			return e
		}
		if len(rows) != 3 || rows[0]["task_id"] != "legacy" || rows[1]["task_id"] != "legacy" || rows[2]["task_id"] != "picked" || rows[2]["connection_id"] != "two" {
			t.Fatalf("selection/rollback: %v", rows)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
