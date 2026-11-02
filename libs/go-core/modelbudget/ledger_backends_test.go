package modelbudget_test

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres/pgtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/modelbudget"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

// Both backends run the same behavior against two independent provider
// instances. PostgreSQL is a temporary real server with a non-superuser,
// non-BYPASSRLS application role, not a mock of its transaction behavior.
func eachBackend(t *testing.T, fn func(*testing.T, func() (modelbudget.Provider, relational.Provider))) {
	t.Helper()
	for _, backend := range []string{"sqlite", "postgres"} {
		t.Run(backend, func(t *testing.T) {
			var open func() (relational.Provider, error)
			if backend == "sqlite" {
				dir := t.TempDir()
				open = func() (relational.Provider, error) { return sqlitestore.New(sqlitestore.Options{Dir: dir}) }
			} else {
				cluster := pgtest.Current(t)
				dsn := cluster.DSN(pgtest.AppRole, cluster.NewDatabase(t))
				open = func() (relational.Provider, error) {
					return postgres.New(context.Background(), postgres.Options{DSN: dsn})
				}
			}
			fn(t, func() (modelbudget.Provider, relational.Provider) {
				p, err := open()
				if err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { _ = p.Close() })
				if err := p.Migrate(context.Background(), modelbudget.Migrations()); err != nil {
					t.Fatal(err)
				}
				if pg, ok := p.(*postgres.Provider); ok {
					audits, err := pg.AuditRLS(context.Background())
					if err != nil || len(audits) != 2 {
						t.Fatalf("RLS audit failed: %v, %+v", err, audits)
					}
					for _, audit := range audits {
						if !audit.Covered() {
							t.Fatalf("ledger table is not tenant isolated: %+v", audit)
						}
					}
				}
				return modelbudget.New(p), p
			})
		})
	}
}

func TestLedgerBackendParity(t *testing.T) {
	eachBackend(t, func(t *testing.T, open func() (modelbudget.Provider, relational.Provider)) {
		ctx := context.Background()
		l1, p1 := open()
		// SQLite migrations run when first bound, before a second process starts.
		if err := l1.Check(ctx, "t1", testTime, limits(10, 10)); err != nil {
			t.Fatal(err)
		}
		l2, _ := open()
		testConcurrentReservations(t, []modelbudget.Provider{l1, l2})
		denied(t, l2.Check(ctx, "t1", testTime, limits(10, 10)))

		policy := limits(100, 200)
		r := reserve(t, l1, "t2", testTime, 100, policy)
		if err := p1.Close(); err != nil {
			t.Fatal(err)
		}
		l3, _ := open()
		denied(t, l3.Check(ctx, "t2", testTime, policy))
		foreign := r
		foreign.TenantID = "t1"
		if err := l3.Settle(ctx, foreign, 0); err == nil {
			t.Fatal("cross-tenant settlement accepted")
		}
		settle(t, l3, r, 30)
		settle(t, l2, r, 30)
		if err := l2.Settle(ctx, r, 31); providererr.CategoryOf(err) != providererr.Conflict {
			t.Fatalf("conflicting settlement = %v", err)
		}
		r2 := reserve(t, l3, "t2", testTime, 70, policy)
		settle(t, l2, r2, 110) // Actual provider charge must survive underestimated reservation.
		denied(t, l3.Check(ctx, "t2", testTime, policy))
		if err := l3.Check(ctx, "t2", testTime.Add(24*time.Hour), policy); err != nil {
			t.Fatal(err)
		}
		reserve(t, l3, "t2", testTime.Add(24*time.Hour), 60, policy)
		denied(t, l2.Check(ctx, "t2", testTime.Add(24*time.Hour), policy))
		if err := l2.Check(ctx, "t2", time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC), policy); err != nil {
			t.Fatal(err)
		}
		denied(t, l2.Check(ctx, "t3", testTime, limits(0, 0)))
		reserve(t, l2, "t3", testTime, 100_000_000, modelbudget.Limits{})
	})
}
