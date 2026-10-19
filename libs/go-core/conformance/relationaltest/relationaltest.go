// Package relationaltest 是 RelationalStore 的一致性测试，与 Python 侧 agentruntime_core.conformance.relational_store 一一对应。
package relationaltest

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
)

// Migrations 是测试用的表结构，与 Python 侧相同。
var Migrations = []relational.Migration{{
	Version: 1,
	Name:    "conformance_items",
	Statements: []string{
		"CREATE TABLE conformance_items (" +
			" id TEXT PRIMARY KEY," +
			" name TEXT NOT NULL UNIQUE," +
			" qty BIGINT NOT NULL DEFAULT 0," +
			" price DOUBLE PRECISION," +
			" note TEXT)",
	},
}}

const insert = "INSERT INTO conformance_items (id, name, qty, price, note) VALUES ($1, $2, $3, $4, $5)"

// Harness 由实现所在包提供。
type Harness interface {
	Provider() relational.Provider
	conformance.Faultable
}

var errUser = errors.New("业务失败")

// Run 跑完整的 RelationalStore 一致性测试。
func Run(t *testing.T, newHarness func(t *testing.T) Harness) {
	ctx := context.Background()
	migrated := func(t *testing.T) Harness {
		h := newHarness(t)
		conformance.NoError(t, h.Provider().Migrate(ctx, Migrations))
		return h
	}
	probe := conformance.Probe[relational.Store]{
		Operation: "Execute",
		Write: func(ctx context.Context, s relational.Store, name string) error {
			return s.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
				_, err := tx.Execute(ctx, insert, name, name, int64(1), nil, nil)
				return err
			})
		},
		Visible: func(ctx context.Context, s relational.Store, name string) (bool, error) {
			found := false
			err := s.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
				_, ok, err := tx.QueryOne(ctx, "SELECT id FROM conformance_items WHERE id = $1", name)
				found = ok
				return err
			})
			return found, err
		},
	}
	binder := func(t *testing.T) conformance.Binder[relational.Store] { return migrated(t).Provider() }
	conformance.RunBindingRules(t, binder)
	conformance.RunIsolationRules(t, binder, probe)
	conformance.RunErrorRules(t, func(t *testing.T) (conformance.Binder[relational.Store], conformance.Faultable) {
		h := migrated(t)
		return h.Provider(), h
	}, probe)

	setup := func(t *testing.T) (Harness, relational.Store) {
		h := migrated(t)
		return h, conformance.Must(h.Provider().Bind(conformance.TenantA()))
	}
	write := func(t *testing.T, s relational.Store, fn func(tx relational.Tx) error) {
		t.Helper()
		conformance.NoError(t, s.Transaction(ctx, relational.TxOptions{}, func(_ context.Context, tx relational.Tx) error { return fn(tx) }))
	}
	queryOne := func(t *testing.T, s relational.Store, sql string, args ...any) (relational.Row, bool) {
		t.Helper()
		var row relational.Row
		var ok bool
		conformance.NoError(t, s.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
			var err error
			row, ok, err = tx.QueryOne(ctx, sql, args...)
			return err
		}))
		return row, ok
	}
	count := func(t *testing.T, s relational.Store) int64 {
		row, _ := queryOne(t, s, "SELECT COUNT(*) AS n FROM conformance_items")
		return row["n"].(int64)
	}

	conformance.Case(t, "CommitAndPortableTypes", func(t *testing.T) {
		_, s := setup(t)
		big := int64(1<<53 + 1)
		write(t, s, func(tx relational.Tx) error {
			n, err := tx.Execute(ctx, insert, "i1", "名称 ✓", big, -0.125, nil)
			if err == nil && n != 1 {
				t.Fatalf("rows affected = %d", n)
			}
			return err
		})
		row, ok := queryOne(t, s, "SELECT name, qty, price, note FROM conformance_items WHERE id = $1", "i1")
		if !ok || row["name"] != "名称 ✓" || row["qty"] != big || row["price"] != -0.125 || row["note"] != nil {
			t.Fatalf("row = %#v", row)
		}
	})
	conformance.Case(t, "ErrorRollsBackAndPropagates", func(t *testing.T) {
		_, s := setup(t)
		err := s.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
			if _, err := tx.Execute(ctx, insert, "i1", "widget", int64(1), nil, nil); err != nil {
				return err
			}
			return errUser
		})
		if !errors.Is(err, errUser) {
			t.Fatalf("err = %v, want the user error unchanged", err)
		}
		if count(t, s) != 0 {
			t.Fatal("transaction was not rolled back")
		}
	})
	conformance.Case(t, "PositionalParametersCanBeReordered", func(t *testing.T) {
		_, s := setup(t)
		write(t, s, func(tx relational.Tx) error {
			_, err := tx.Execute(ctx, insert, "i1", "widget", int64(5), nil, nil)
			return err
		})
		row, ok := queryOne(t, s, "SELECT id FROM conformance_items WHERE name = $2 AND qty = $1", int64(5), "widget")
		if !ok || row["id"] != "i1" {
			t.Fatalf("row = %#v", row)
		}
	})
	conformance.Case(t, "QueryOneWithSeveralRowsIsInvalid", func(t *testing.T) {
		_, s := setup(t)
		write(t, s, func(tx relational.Tx) error {
			if _, err := tx.Execute(ctx, insert, "i1", "a", int64(1), nil, nil); err != nil {
				return err
			}
			_, err := tx.Execute(ctx, insert, "i2", "b", int64(1), nil, nil)
			return err
		})
		err := s.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
			_, _, err := tx.QueryOne(ctx, "SELECT id FROM conformance_items")
			return err
		})
		conformance.ExpectError(t, err, providererr.Invalid, "")
	})
	conformance.Case(t, "UniqueViolationIsConflict", func(t *testing.T) {
		_, s := setup(t)
		write(t, s, func(tx relational.Tx) error {
			_, err := tx.Execute(ctx, insert, "i1", "widget", int64(1), nil, nil)
			return err
		})
		err := s.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
			_, err := tx.Execute(ctx, insert, "i2", "widget", int64(1), nil, nil)
			return err
		})
		conformance.ExpectError(t, err, providererr.Conflict, "")
	})
	conformance.Case(t, "BadSQLIsInvalid", func(t *testing.T) {
		_, s := setup(t)
		for _, sql := range []string{"SELEC id FROM conformance_items", "SELECT id FROM no_such_table", "SELECT no_such_column FROM conformance_items"} {
			err := s.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
				_, err := tx.Query(ctx, sql)
				return err
			})
			conformance.ExpectError(t, err, providererr.Invalid, "")
		}
	})
	conformance.Case(t, "WriteInReadOnlyTransactionIsInvalid", func(t *testing.T) {
		_, s := setup(t)
		err := s.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
			_, err := tx.Execute(ctx, insert, "i1", "widget", int64(1), nil, nil)
			return err
		})
		conformance.ExpectError(t, err, providererr.Invalid, "")
		if count(t, s) != 0 {
			t.Fatal("read-only transaction wrote a row")
		}
	})
	conformance.Case(t, "UpsertIsIdempotent", func(t *testing.T) {
		_, s := setup(t)
		write(t, s, func(tx relational.Tx) error {
			first, err := tx.Execute(ctx, insert+" ON CONFLICT (id) DO NOTHING", "i1", "widget", int64(1), nil, nil)
			if err != nil {
				return err
			}
			second, err := tx.Execute(ctx, insert+" ON CONFLICT (id) DO NOTHING", "i1", "widget", int64(1), nil, nil)
			if err == nil && (first != 1 || second != 0) {
				t.Fatalf("rows affected = %d then %d", first, second)
			}
			if err != nil {
				return err
			}
			_, err = tx.Execute(ctx, insert+" ON CONFLICT (id) DO UPDATE SET qty = excluded.qty", "i1", "widget", int64(7), nil, nil)
			return err
		})
		row, _ := queryOne(t, s, "SELECT qty FROM conformance_items WHERE id = $1", "i1")
		if row["qty"] != int64(7) || count(t, s) != 1 {
			t.Fatalf("row = %#v", row)
		}
	})
	conformance.Case(t, "DeleteRemovesRows", func(t *testing.T) {
		_, s := setup(t)
		write(t, s, func(tx relational.Tx) error {
			_, err := tx.Execute(ctx, insert, "i1", "widget", int64(1), nil, nil)
			return err
		})
		write(t, s, func(tx relational.Tx) error {
			first, err := tx.Execute(ctx, "DELETE FROM conformance_items WHERE id = $1", "i1")
			if err != nil {
				return err
			}
			second, err := tx.Execute(ctx, "DELETE FROM conformance_items WHERE id = $1", "i1")
			if err == nil && (first != 1 || second != 0) {
				t.Fatalf("rows deleted = %d then %d", first, second)
			}
			return err
		})
		if count(t, s) != 0 {
			t.Fatal("row survived DELETE")
		}
	})
	conformance.Case(t, "ConcurrentIncrementsAreNotLost", func(t *testing.T) {
		_, s := setup(t)
		write(t, s, func(tx relational.Tx) error {
			_, err := tx.Execute(ctx, insert, "i1", "counter", int64(0), nil, nil)
			return err
		})
		var wg sync.WaitGroup
		errs := make(chan error, 20)
		for range 20 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				errs <- s.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
					_, err := tx.Execute(ctx, "UPDATE conformance_items SET qty = qty + 1 WHERE id = $1", "i1")
					return err
				})
			}()
		}
		wg.Wait()
		close(errs)
		for err := range errs {
			conformance.NoError(t, err)
		}
		if row, _ := queryOne(t, s, "SELECT qty FROM conformance_items WHERE id = $1", "i1"); row["qty"] != int64(20) {
			t.Fatalf("qty = %v, want 20", row["qty"])
		}
	})
	conformance.Case(t, "MigrateIsIdempotentAndCoversNewTenants", func(t *testing.T) {
		h, _ := setup(t)
		conformance.NoError(t, h.Provider().Migrate(ctx, Migrations))
		late := conformance.Must(h.Provider().Bind(conformance.Tenant("t-gamma")))
		write(t, late, func(tx relational.Tx) error {
			_, err := tx.Execute(ctx, insert, "i1", "widget", int64(1), nil, nil)
			return err
		})
		if count(t, late) != 1 {
			t.Fatal("late tenant cannot use migrated tables")
		}
	})
	conformance.Case(t, "OtherTenantWritesDoNotCollide", func(t *testing.T) {
		h, s := setup(t)
		other := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		for _, store := range []relational.Store{s, other} {
			write(t, store, func(tx relational.Tx) error {
				_, err := tx.Execute(ctx, insert, "i1", "widget", int64(1), nil, nil)
				return err
			})
		}
		if count(t, s) != 1 || count(t, other) != 1 {
			t.Fatal("tenants see each other's rows")
		}
	})
}
