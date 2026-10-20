package postgres_test

// S-12a 验收：业务库的租户隔离（设计书 4.3“按租户分区的表、行级安全”；计划 S-12a 交付与难点）。
//
//   - 与开发实现同一套一致性测试（relationaltest），含租户绑定与隔离规则。
//   - 迁移出来的每张表都有租户列、开启并强制了行级安全、有按会话变量过滤的策略，唯一约束以租户开头
//     （验收“RLS 策略覆盖所有带租户数据的表”的自动核对；AuditRLS 也供运维抽查）。
//   - 缺少租户条件的查询什么都看不到、写不进；应用 SQL 改不了租户。
//   - RLS 与连接池：会话变量只在事务内有效，连接归还时清除；并发的不同租户互不可见（难点“RLS 与连接池”）。
//   - 在线服务的角色不能绕过行级安全；外键不受支持。
//
// 用临时的 Postgres 实例（pgtest，二进制取自 PATH 或 AR_POSTGRES_BIN），原始 SQL 经 psql 执行。

import (
	"context"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres/pgtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/relationaltest"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

var (
	t1 = &commonv1.TenantContext{TenantId: "t1"}
	t2 = &commonv1.TenantContext{TenantId: "t2"}
)

type harness struct {
	provider *postgres.Provider
	faults   *testkit.Faults
}

func (h *harness) Provider() relational.Provider { return h.provider }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}

// newProvider 在一个新库里以在线服务角色构造 Provider。
func newProvider(t *testing.T, opts postgres.Options) (*postgres.Provider, *pgtest.Cluster, string) {
	t.Helper()
	c := pgtest.Current(t)
	db := c.NewDatabase(t)
	opts.DSN = c.DSN(pgtest.AppRole, db)
	p, err := postgres.New(context.Background(), opts)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	return p, c, db
}

func TestConformance(t *testing.T) {
	relationaltest.Run(t, func(t *testing.T) relationaltest.Harness {
		h := &harness{faults: testkit.NewFaults()}
		h.provider, _, _ = newProvider(t, postgres.Options{Faults: h.faults})
		return h
	})
}

// 业务库的几种典型结构：建表、唯一约束、唯一索引、后续版本加列（task-service 的 taskstore 就是这样演进的）。
// 版本号接在一致性测试的迁移（版本 1）后面：版本全库共用，同一个版本号只执行一次。
var schema = []relational.Migration{{
	Version: 10,
	Name:    "sessions_and_tasks",
	Statements: []string{
		`CREATE TABLE sessions (session_id TEXT PRIMARY KEY, created_at BIGINT NOT NULL)`,
		`CREATE TABLE tasks (task_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, client_request_id TEXT NOT NULL,
			status BIGINT NOT NULL, UNIQUE (session_key, client_request_id))`,
	},
}, {
	Version: 11,
	Name:    "task_outcome_and_index",
	Statements: []string{
		`ALTER TABLE tasks ADD COLUMN outcome TEXT NOT NULL DEFAULT ''`,
		`CREATE TABLE events (event_id BIGINT NOT NULL, task_id TEXT NOT NULL, body TEXT)`,
		`CREATE UNIQUE INDEX events_by_task ON events (task_id, event_id)`,
	},
}}

func migrate(t *testing.T, p *postgres.Provider, ms ...[]relational.Migration) {
	t.Helper()
	for _, m := range ms {
		if err := p.Migrate(context.Background(), m); err != nil {
			t.Fatal(err)
		}
	}
}

func tx(t *testing.T, p *postgres.Provider, tenant *commonv1.TenantContext, fn func(tx relational.Tx) error) error {
	t.Helper()
	s, err := p.Bind(tenant)
	if err != nil {
		t.Fatal(err)
	}
	return s.Transaction(context.Background(), relational.TxOptions{}, func(_ context.Context, tx relational.Tx) error { return fn(tx) })
}

func mustTx(t *testing.T, p *postgres.Provider, tenant *commonv1.TenantContext, fn func(tx relational.Tx) error) {
	t.Helper()
	if err := tx(t, p, tenant, fn); err != nil {
		t.Fatal(err)
	}
}

func insertTask(t *testing.T, p *postgres.Provider, tenant *commonv1.TenantContext, id, crid string) {
	t.Helper()
	mustTx(t, p, tenant, func(tx relational.Tx) error {
		_, err := tx.Execute(context.Background(), `INSERT INTO tasks (task_id, session_key, client_request_id, status) VALUES ($1, '', $2, 0)`, id, crid)
		return err
	})
}

func TestEveryMigratedTableIsCoveredByRowLevelSecurity(t *testing.T) {
	p, c, db := newProvider(t, postgres.Options{})
	migrate(t, p, relationaltest.Migrations, schema)
	audits, err := p.AuditRLS(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, a := range audits {
		seen[a.Table] = true
		if !a.Covered() {
			t.Errorf("表 %s 的租户隔离不完整：%+v", a.Table, a)
		}
	}
	for _, want := range []string{"conformance_items", "sessions", "tasks", "events"} {
		if !seen[want] {
			t.Errorf("AuditRLS 漏了表 %s：%v", want, audits)
		}
	}
	// 用超级用户直接看系统表，不只信 AuditRLS 自己的结论。
	rows := c.MustPsql(t, "postgres", db, `SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
		EXISTS (SELECT 1 FROM pg_policy pol WHERE pol.polrelid = c.oid AND pol.polname = 'agentruntime_tenant')
		FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE c.relkind = 'r' AND n.nspname = current_schema() AND c.relname <> 'agentruntime_migrations' ORDER BY 1`)
	for _, line := range strings.Split(rows, "\n") {
		if line != "" && !strings.HasSuffix(line, "|t|t|t") {
			t.Errorf("系统表显示行级安全没开全：%s", line)
		}
	}
	uniques := c.MustPsql(t, "postgres", db, `SELECT i.indexrelid::regclass, a.attname
		FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace
		JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
		WHERE i.indisunique AND n.nspname = current_schema() AND c.relname <> 'agentruntime_migrations'`)
	for _, line := range strings.Split(uniques, "\n") {
		if line != "" && !strings.HasSuffix(line, "|tenant_id") {
			t.Errorf("唯一约束应以 tenant_id 开头：%s", line)
		}
	}
}

func TestQueriesWithoutTheTenantSettingSeeNothingAndCannotWrite(t *testing.T) {
	p, c, db := newProvider(t, postgres.Options{})
	migrate(t, p, schema)
	insertTask(t, p, t1, "tsk_a", "c1")
	// 绕过 Provider、以同一个在线服务角色直接连库：没有租户会话变量。
	if got := c.MustPsql(t, pgtest.AppRole, db, "SELECT count(*) FROM tasks"); got != "0" {
		t.Errorf("没有租户条件时应当一行都看不到，实际 %s", got)
	}
	if out, err := c.Psql(pgtest.AppRole, db, "INSERT INTO tasks (task_id, session_key, client_request_id, status) VALUES ('tsk_x', '', 'c9', 0)"); err == nil {
		t.Errorf("没有租户条件时应当写不进：%s", out)
	}
	if got := c.MustPsql(t, pgtest.AppRole, db, "UPDATE tasks SET status = 9"); got != "" {
		t.Errorf("没有租户条件的更新不该改到任何行：%s", got)
	}
	if got := c.MustPsql(t, "postgres", db, "SELECT status FROM tasks WHERE task_id = 'tsk_a'"); got != "0" {
		t.Errorf("t1 的行不该被改动，实际 status=%s", got)
	}
}

func TestATenantCannotReadOrChangeAnotherTenantsRows(t *testing.T) {
	p, _, _ := newProvider(t, postgres.Options{})
	migrate(t, p, schema)
	insertTask(t, p, t1, "tsk_a", "c1")
	mustTx(t, p, t2, func(tx relational.Tx) error {
		ctx := context.Background()
		rows, err := tx.Query(ctx, "SELECT task_id FROM tasks")
		if err != nil || len(rows) != 0 {
			t.Errorf("t2 不该看到 t1 的行：%v（%v）", rows, err)
		}
		n, err := tx.Execute(ctx, "UPDATE tasks SET status = 7")
		if err != nil || n != 0 {
			t.Errorf("t2 的更新不该改到 t1 的行：%d（%v）", n, err)
		}
		n, err = tx.Execute(ctx, "DELETE FROM tasks")
		if err != nil || n != 0 {
			t.Errorf("t2 的删除不该删到 t1 的行：%d（%v）", n, err)
		}
		// 同一个主键、同一个唯一键：各租户各自一份，不冲突，也就不泄露 t1 有这一行。
		_, err = tx.Execute(ctx, `INSERT INTO tasks (task_id, session_key, client_request_id, status) VALUES ('tsk_a', '', 'c1', 5)`)
		if err != nil {
			t.Errorf("t2 用同样的键插入不该冲突：%v", err)
		}
		return nil
	})
	mustTx(t, p, t1, func(tx relational.Tx) error {
		row, ok, err := tx.QueryOne(context.Background(), "SELECT status FROM tasks WHERE task_id = 'tsk_a'")
		if err != nil || !ok || row["status"] != int64(0) {
			t.Errorf("t1 的行应当原样保留：%v %v %v", row, ok, err)
		}
		return nil
	})
}

func TestApplicationSQLCannotTouchTheTenant(t *testing.T) {
	p, _, _ := newProvider(t, postgres.Options{})
	migrate(t, p, schema)
	for _, sql := range []string{
		"SELECT set_config('agentruntime.tenant_id', 't2', true)",
		"SET agentruntime.tenant_id = 't2'",
		"set local agentruntime.tenant_id to 't2'",
		"RESET agentruntime.tenant_id",
		"SELECT current_setting('agentruntime.tenant_id')",
		"SELECT task_id FROM tasks WHERE tenant_id = 't2'",
	} {
		err := tx(t, p, t1, func(tx relational.Tx) error {
			_, err := tx.Execute(context.Background(), sql)
			return err
		})
		if providererr.CategoryOf(err) != providererr.Invalid {
			t.Errorf("%q 应当报 Invalid，实际 %v", sql, err)
		}
	}
}

func TestTheTenantSettingDoesNotOutliveTheTransaction(t *testing.T) {
	// 连接池只有一个连接：t1 的事务结束后，同一个连接上不再有 t1 的会话变量。
	p, _, _ := newProvider(t, postgres.Options{MaxConns: 1})
	migrate(t, p, schema)
	insertTask(t, p, t1, "tsk_a", "c1")
	_ = tx(t, p, t1, func(tx relational.Tx) error { return fmt.Errorf("业务失败，回滚") })
	mustTx(t, p, t2, func(tx relational.Tx) error {
		rows, err := tx.Query(context.Background(), "SELECT task_id FROM tasks")
		if err != nil || len(rows) != 0 {
			t.Errorf("复用的连接不该带着 t1 的租户：%v（%v）", rows, err)
		}
		return nil
	})
	if got, err := p.ConnectionTenantForTest(context.Background()); err != nil || got != "" {
		t.Errorf("归还连接池的连接上会话变量应为空，实际 %q（%v）", got, err)
	}
}

func TestConcurrentTenantsNeverSeeEachOther(t *testing.T) {
	p, _, _ := newProvider(t, postgres.Options{MaxConns: 4})
	migrate(t, p, schema)
	const perTenant = 40
	var wg sync.WaitGroup
	errs := make(chan error, 4*perTenant)
	for _, tenant := range []*commonv1.TenantContext{t1, t2} {
		for i := range perTenant {
			wg.Add(1)
			go func() {
				defer wg.Done()
				id := fmt.Sprintf("tsk_%s_%d", tenant.GetTenantId(), i)
				errs <- tx(t, p, tenant, func(tx relational.Tx) error {
					ctx := context.Background()
					if _, err := tx.Execute(ctx, `INSERT INTO tasks (task_id, session_key, client_request_id, status) VALUES ($1, '', $1, 0)`, id); err != nil {
						return err
					}
					rows, err := tx.Query(ctx, "SELECT task_id FROM tasks")
					if err != nil {
						return err
					}
					for _, r := range rows {
						if !strings.HasPrefix(r["task_id"].(string), "tsk_"+tenant.GetTenantId()+"_") {
							return fmt.Errorf("%s 看到了别的租户的行 %v", tenant.GetTenantId(), r["task_id"])
						}
					}
					return nil
				})
			}()
		}
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Error(err)
		}
	}
	for _, tenant := range []*commonv1.TenantContext{t1, t2} {
		mustTx(t, p, tenant, func(tx relational.Tx) error {
			row, _, err := tx.QueryOne(context.Background(), "SELECT count(*) AS n FROM tasks")
			if err != nil || row["n"] != int64(perTenant) {
				t.Errorf("%s 应当正好有 %d 行，实际 %v（%v）", tenant.GetTenantId(), perTenant, row, err)
			}
			return nil
		})
	}
}

func TestForeignKeysAreRejected(t *testing.T) {
	p, _, _ := newProvider(t, postgres.Options{})
	err := p.Migrate(context.Background(), []relational.Migration{{
		Version: 1, Name: "fk",
		Statements: []string{
			"CREATE TABLE parents (id TEXT PRIMARY KEY)",
			"CREATE TABLE children (id TEXT PRIMARY KEY, parent_id TEXT REFERENCES parents (id))",
		},
	}})
	if providererr.CategoryOf(err) != providererr.Invalid {
		t.Errorf("外键检查会越过行级安全，迁移里出现外键应报 Invalid，实际 %v", err)
	}
}

func TestTheOnlineRoleMustNotBypassRowLevelSecurity(t *testing.T) {
	c := pgtest.Current(t)
	db := c.NewDatabase(t)
	c.EnsureRole(t, "s12a_bypass", "NOSUPERUSER BYPASSRLS")
	for _, dsn := range []string{c.SuperuserDSN(db), c.DSN("s12a_bypass", db)} {
		p, err := postgres.New(context.Background(), postgres.Options{DSN: dsn})
		if err == nil {
			_ = p.Close()
			t.Errorf("以 %s 连接应当被拒：在线服务的角色不能绕过行级安全", dsn)
		} else if strings.Contains(err.Error(), "@127.0.0.1") {
			t.Errorf("报错不该带出连接串：%v", err)
		}
	}
}

func TestPlatformScopeIsSeparateFromTenants(t *testing.T) {
	p, _, _ := newProvider(t, postgres.Options{})
	migrate(t, p, schema)
	platform, err := p.BindPlatform("registry")
	if err != nil {
		t.Fatal(err)
	}
	conformance.NoError(t, platform.Transaction(context.Background(), relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `INSERT INTO tasks (task_id, session_key, client_request_id, status) VALUES ('tsk_p', '', 'cp', 0)`)
		return err
	}))
	mustTx(t, p, t1, func(tx relational.Tx) error {
		rows, err := tx.Query(context.Background(), "SELECT task_id FROM tasks")
		if err != nil || len(rows) != 0 {
			t.Errorf("租户看不到平台作用域的行：%v（%v）", rows, err)
		}
		return nil
	})
}
