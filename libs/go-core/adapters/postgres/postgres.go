// Package postgres 是 RelationalStore 的 Postgres 实现：一个库、共享的表，租户隔离靠行级安全（RLS）
// （设计书 4.3；计划 S-12a）。语义与开发实现（sqlitestore）相同，由同一套一致性测试保证（relationaltest）。
//
// 隔离的做法：
//   - 应用 SQL 不写租户条件，也不出现租户列（providers/relational）。Migrate 执行完每个版本后，给模式里每张表补上
//     tenant_id 列（默认值取会话变量 agentruntime.tenant_id），开启并强制行级安全（FORCE，表的属主也受约束），
//     建策略 agentruntime_tenant：只看得到、只写得进 tenant_id 等于会话变量的行。会话变量没设（或为空）时什么都看不到、
//     写不进。表上不允许有别的策略（宽松策略之间是“或”，会放开限制）。
//   - 主键与唯一约束（含唯一索引）改写成以 tenant_id 开头：不同租户的同一个键互不冲突，冲突也不会泄露别人有这一行。
//     应用 SQL 里 ON CONFLICT (列…) 的冲突目标随之自动补上 tenant_id。
//   - 不支持外键（迁移里出现外键报 Invalid）：外键检查会越过行级安全。
//   - 事务开始时用 set_config('agentruntime.tenant_id', <作用域>, true) 设置会话变量，只在本事务内有效；
//     连接归还连接池时再核对一次，会话变量不为空的连接直接丢弃，不会串到下一个租户。
//   - 应用 SQL 不能碰租户：出现 tenant_id、set_config、current_setting、agentruntime. 或以 SET、RESET 开头的语句报 Invalid。
//     查询结果里也不带 tenant_id 列。
//   - 在线服务用的数据库角色不能是超级用户，也不能带 BYPASSRLS（New 时核对）。离线任务需要跨租户读取时用单独的角色
//     并留审计（计划 S-12 难点），不在本包内。
//
// 平台作用域的会话变量取 _platform，与租户 ID 不会冲突。迁移记录在 agentruntime_migrations（不分租户）。
package postgres

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// 行级安全用到的名字。
const (
	TenantSetting   = "agentruntime.tenant_id"
	TenantColumn    = "tenant_id"
	PolicyName      = "agentruntime_tenant"
	MigrationsTable = "agentruntime_migrations"
)

const (
	// policyExpr 是策略的条件：会话变量为空等同于没设，任何行都不匹配。
	policyExpr = "tenant_id = NULLIF(current_setting('agentruntime.tenant_id', true), '')"
	// columnDefault 取不到会话变量时报错，没有租户的写入因此失败。
	columnDefault = "current_setting('agentruntime.tenant_id')"
	// migrateLock 让多个副本同时启动时迁移只跑一次。
	migrateLock = "agentruntime.migrate"
)

var (
	// forbidden 是应用 SQL 里不许出现的写法：碰租户列、碰会话变量、切换角色或参数。
	forbidden  = regexp.MustCompile(`(?is)\b(tenant_id|set_config|current_setting)\b|\bagentruntime\.|^\s*(set|reset)\b`)
	onConflict = regexp.MustCompile(`(?is)\bON\s+CONFLICT\s*\(`)
)

// Options 是构造参数。
type Options struct {
	// DSN 是在线服务的连接串（不能是超级用户或带 BYPASSRLS 的角色）。
	DSN string
	// MaxConns 是连接池的上限；0 表示 8。
	MaxConns int32
	// Faults 供测试注入错误（操作名与 sqlitestore 相同：Transaction、Execute、Query、QueryOne）。
	Faults *testkit.Faults
}

// Provider 是 Postgres RelationalStore 的工厂。
type Provider struct {
	pool   *pgxpool.Pool
	faults *testkit.Faults
}

// New 连接数据库并核对角色。报错里不带连接串。
func New(ctx context.Context, opts Options) (*Provider, error) {
	if opts.DSN == "" {
		return nil, errors.New("postgres: 缺少连接串")
	}
	cfg, err := pgxpool.ParseConfig(opts.DSN)
	if err != nil {
		return nil, errors.New("postgres: 连接串格式不对")
	}
	cfg.MaxConns = 8
	if opts.MaxConns > 0 {
		cfg.MaxConns = opts.MaxConns
	}
	cfg.AfterRelease = func(c *pgx.Conn) bool {
		// 会话变量只在事务内有效；归还时再核对一次，不为空（例如事务没有正常结束）就丢弃这个连接。
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		var v string
		if err := c.QueryRow(ctx, "SELECT coalesce(current_setting($1, true), '')", TenantSetting).Scan(&v); err != nil {
			return false
		}
		return v == ""
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, providererr.New(providererr.Transient, "unavailable", "连不上业务库")
	}
	var super, bypass bool
	err = pool.QueryRow(ctx, "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").Scan(&super, &bypass)
	if err != nil {
		pool.Close()
		return nil, providererr.New(providererr.Transient, "unavailable", "连不上业务库")
	}
	if super || bypass {
		pool.Close()
		return nil, providererr.New(providererr.Denied, "role_bypasses_rls", "在线服务的数据库角色不能是超级用户，也不能带 BYPASSRLS")
	}
	return &Provider{pool: pool, faults: opts.Faults}, nil
}

func (p *Provider) Bind(t *commonv1.TenantContext) (relational.Store, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &store{p: p, owner: devutil.ScopeID(s)}, nil
}

func (p *Provider) BindPlatform(purpose string) (relational.Store, error) {
	s, err := tenancy.PlatformScope(purpose)
	if err != nil {
		return nil, err
	}
	return &store{p: p, owner: devutil.ScopeID(s)}, nil
}

func (p *Provider) Close() error {
	p.pool.Close()
	return nil
}

// MapError 把 Postgres 的错误映射到统一的错误模型；nil 原样返回。
func MapError(err error) error {
	if err == nil {
		return nil
	}
	var pe *pgconn.PgError
	if !errors.As(err, &pe) {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return providererr.Wrap(providererr.Transient, "timeout", "数据库操作超时或被取消", err)
		}
		return providererr.Wrap(providererr.Transient, "unavailable", "业务库暂时不可用", err)
	}
	switch {
	case pe.Code == "23505":
		return providererr.Wrap(providererr.Conflict, "unique_violation", "唯一约束冲突", err)
	case strings.HasPrefix(pe.Code, "23"):
		return providererr.Wrap(providererr.Invalid, "constraint_violation", "违反约束", err)
	case pe.Code == "25006":
		return providererr.Wrap(providererr.Invalid, "read_only", "只读事务里不能写入", err)
	case pe.Code == "42501":
		return providererr.Wrap(providererr.Denied, "row_level_security", "没有权限，或违反行级安全", err)
	case pe.Code == "40001", pe.Code == "40P01":
		return providererr.Wrap(providererr.Transient, "serialization_failure", "事务冲突，可重试", err)
	case strings.HasPrefix(pe.Code, "08"), strings.HasPrefix(pe.Code, "57"), strings.HasPrefix(pe.Code, "53"):
		return providererr.Wrap(providererr.Transient, "unavailable", "业务库暂时不可用", err)
	default:
		return providererr.Wrap(providererr.Invalid, "bad_sql", "SQL 不合法", err)
	}
}

func ident(name string) string { return pgx.Identifier{name}.Sanitize() }

// --- 迁移 ---

// Migrate 按版本执行结构变更，并给每张表补上租户列、行级安全与以租户开头的唯一约束。
func (p *Provider) Migrate(ctx context.Context, migrations []relational.Migration) error {
	if _, err := p.pool.Exec(ctx, "CREATE TABLE IF NOT EXISTS "+MigrationsTable+" (version BIGINT PRIMARY KEY, name TEXT NOT NULL)"); err != nil {
		return MapError(err)
	}
	sorted := append([]relational.Migration(nil), migrations...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Version < sorted[j].Version })
	for _, m := range sorted {
		if err := p.migrateOne(ctx, m); err != nil {
			return err
		}
	}
	return nil
}

func (p *Provider) migrateOne(ctx context.Context, m relational.Migration) error {
	tx, err := p.pool.Begin(ctx)
	if err != nil {
		return MapError(err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1))", migrateLock); err != nil {
		return MapError(err)
	}
	var done bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS (SELECT 1 FROM "+MigrationsTable+" WHERE version = $1)", m.Version).Scan(&done); err != nil {
		return MapError(err)
	}
	if done {
		return nil
	}
	for _, stmt := range m.Statements {
		if _, err := tx.Exec(ctx, stmt); err != nil {
			return MapError(err)
		}
	}
	if err := secure(ctx, tx); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, "INSERT INTO "+MigrationsTable+" (version, name) VALUES ($1, $2)", m.Version, m.Name); err != nil {
		return MapError(err)
	}
	return MapError(tx.Commit(ctx))
}

type table struct {
	oid  uint32
	name string
}

func tables(ctx context.Context, q pgx.Tx) ([]table, error) {
	rows, err := q.Query(ctx, `SELECT c.oid, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE c.relkind IN ('r', 'p') AND n.nspname = current_schema() AND c.relname <> $1 ORDER BY c.relname`, MigrationsTable)
	if err != nil {
		return nil, MapError(err)
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (table, error) {
		var t table
		return t, r.Scan(&t.oid, &t.name)
	})
}

func unsupported(msg string) error {
	return providererr.New(providererr.Invalid, "unsupported_schema", msg)
}

// secure 给模式里每张表补上租户隔离。只在迁移的事务里调用，可以重复执行。
func secure(ctx context.Context, tx pgx.Tx) error {
	var fks int
	if err := tx.QueryRow(ctx, `SELECT count(*) FROM pg_constraint con JOIN pg_namespace n ON n.oid = con.connamespace
		WHERE con.contype = 'f' AND n.nspname = current_schema()`).Scan(&fks); err != nil {
		return MapError(err)
	}
	if fks > 0 {
		return unsupported("业务库不支持外键：外键检查会越过行级安全")
	}
	ts, err := tables(ctx, tx)
	if err != nil {
		return err
	}
	for _, t := range ts {
		if err := secureTable(ctx, tx, t); err != nil {
			return err
		}
	}
	return nil
}

func secureTable(ctx context.Context, tx pgx.Tx, t table) error {
	name := ident(t.name)
	var hasColumn bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = $1 AND attname = $2 AND NOT attisdropped)", t.oid, TenantColumn).Scan(&hasColumn); err != nil {
		return MapError(err)
	}
	stmts := []string{}
	if !hasColumn {
		// 默认值分开设：ADD COLUMN ... DEFAULT 会当场求一次默认值（迁移时没有租户会话变量）。
		// 迁移里写入的行没有租户，SET NOT NULL 会失败：业务库的表不放不属于任何租户的数据。
		stmts = append(stmts,
			fmt.Sprintf("ALTER TABLE %s ADD COLUMN %s TEXT", name, TenantColumn),
			fmt.Sprintf("ALTER TABLE %s ALTER COLUMN %s SET DEFAULT %s", name, TenantColumn, columnDefault),
			fmt.Sprintf("ALTER TABLE %s ALTER COLUMN %s SET NOT NULL", name, TenantColumn))
	}
	stmts = append(stmts,
		fmt.Sprintf("ALTER TABLE %s ENABLE ROW LEVEL SECURITY", name),
		fmt.Sprintf("ALTER TABLE %s FORCE ROW LEVEL SECURITY", name))
	for _, s := range stmts {
		if _, err := tx.Exec(ctx, s); err != nil {
			return MapError(err)
		}
	}
	// 策略：只能有我们的一条。
	rows, err := tx.Query(ctx, "SELECT polname FROM pg_policy WHERE polrelid = $1", t.oid)
	if err != nil {
		return MapError(err)
	}
	policies, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return MapError(err)
	}
	ours := false
	for _, pol := range policies {
		if pol != PolicyName {
			return unsupported(fmt.Sprintf("表 %s 上不能有别的行级安全策略（%s）", t.name, pol))
		}
		ours = true
	}
	if !ours {
		if _, err := tx.Exec(ctx, fmt.Sprintf("CREATE POLICY %s ON %s USING (%s) WITH CHECK (%s)", PolicyName, name, policyExpr, policyExpr)); err != nil {
			return MapError(err)
		}
	}
	return scopeUniqueKeys(ctx, tx, t)
}

type uniqueKey struct {
	index      string
	constraint string // 为空表示独立的唯一索引
	primary    bool
	columns    []string
	predicate  string
	first      string
	expression bool
}

// scopeUniqueKeys 把不以 tenant_id 开头的主键、唯一约束与唯一索引改写成以 tenant_id 开头。
func scopeUniqueKeys(ctx context.Context, tx pgx.Tx, t table) error {
	rows, err := tx.Query(ctx, `SELECT ic.relname, coalesce(con.conname, ''), i.indisprimary,
			array(SELECT coalesce(a.attname, '') FROM unnest(i.indkey) WITH ORDINALITY k(attnum, ord)
				LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum ORDER BY k.ord),
			coalesce(pg_get_expr(i.indpred, i.indrelid), ''), i.indexprs IS NOT NULL
		FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
		LEFT JOIN pg_constraint con ON con.conindid = i.indexrelid AND con.contype IN ('p', 'u')
		WHERE i.indrelid = $1 AND i.indisunique ORDER BY ic.relname`, t.oid)
	if err != nil {
		return MapError(err)
	}
	keys, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (uniqueKey, error) {
		var k uniqueKey
		err := r.Scan(&k.index, &k.constraint, &k.primary, &k.columns, &k.predicate, &k.expression)
		if len(k.columns) > 0 {
			k.first = k.columns[0]
		}
		return k, err
	})
	if err != nil {
		return MapError(err)
	}
	name := ident(t.name)
	for _, k := range keys {
		if k.first == TenantColumn {
			continue
		}
		if k.expression {
			return unsupported(fmt.Sprintf("表 %s 的唯一索引 %s 含表达式，无法按租户改写", t.name, k.index))
		}
		cols := []string{TenantColumn}
		for _, c := range k.columns {
			cols = append(cols, ident(c))
		}
		var stmt string
		switch {
		case k.constraint != "" && k.primary:
			stmt = fmt.Sprintf("ALTER TABLE %s DROP CONSTRAINT %s, ADD CONSTRAINT %s PRIMARY KEY (%s)", name, ident(k.constraint), ident(k.constraint), strings.Join(cols, ", "))
		case k.constraint != "":
			stmt = fmt.Sprintf("ALTER TABLE %s DROP CONSTRAINT %s, ADD CONSTRAINT %s UNIQUE (%s)", name, ident(k.constraint), ident(k.constraint), strings.Join(cols, ", "))
		default:
			stmt = fmt.Sprintf("DROP INDEX %s; CREATE UNIQUE INDEX %s ON %s (%s)", ident(k.index), ident(k.index), name, strings.Join(cols, ", "))
			if k.predicate != "" {
				stmt += " WHERE " + k.predicate
			}
		}
		for _, s := range strings.Split(stmt, "; ") {
			if _, err := tx.Exec(ctx, s); err != nil {
				return MapError(err)
			}
		}
	}
	return nil
}

// --- 核对 ---

// TableAudit 是一张表的行级安全情况（计划 S-12a 验收“RLS 策略覆盖所有带租户数据的表”的自动核对）。
type TableAudit struct {
	Table string
	// HasTenantColumn：有 tenant_id 列且不允许为空。
	HasTenantColumn bool
	// RLSEnabled、RLSForced：开启并强制了行级安全。
	RLSEnabled bool
	RLSForced  bool
	// HasPolicy：只有 agentruntime_tenant 一条策略，读写都按租户会话变量过滤。
	HasPolicy bool
	// UnscopedUniqueKeys：不以 tenant_id 开头的主键、唯一约束或唯一索引的名字。
	UnscopedUniqueKeys []string
}

// Covered 表示这张表的租户隔离完整。
func (a TableAudit) Covered() bool {
	return a.HasTenantColumn && a.RLSEnabled && a.RLSForced && a.HasPolicy && len(a.UnscopedUniqueKeys) == 0
}

// AuditRLS 列出模式里每张业务表（迁移记录表除外）的行级安全情况。
func (p *Provider) AuditRLS(ctx context.Context) ([]TableAudit, error) {
	tx, err := p.pool.BeginTx(ctx, pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, MapError(err)
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	ts, err := tables(ctx, tx)
	if err != nil {
		return nil, err
	}
	var out []TableAudit
	for _, t := range ts {
		a := TableAudit{Table: t.name}
		err := tx.QueryRow(ctx, `SELECT c.relrowsecurity, c.relforcerowsecurity,
				EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = c.oid AND attname = $2 AND attnotnull AND NOT attisdropped),
				(SELECT count(*) FROM pg_policy WHERE polrelid = c.oid) = 1 AND
				EXISTS (SELECT 1 FROM pg_policy WHERE polrelid = c.oid AND polname = $3 AND polcmd = '*' AND polpermissive
					AND pg_get_expr(polqual, polrelid) IS NOT NULL AND pg_get_expr(polwithcheck, polrelid) IS NOT NULL)
			FROM pg_class c WHERE c.oid = $1`, t.oid, TenantColumn, PolicyName).Scan(&a.RLSEnabled, &a.RLSForced, &a.HasTenantColumn, &a.HasPolicy)
		if err != nil {
			return nil, MapError(err)
		}
		rows, err := tx.Query(ctx, `SELECT ic.relname FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
			LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
			WHERE i.indrelid = $1 AND i.indisunique AND coalesce(a.attname, '') <> $2 ORDER BY 1`, t.oid, TenantColumn)
		if err != nil {
			return nil, MapError(err)
		}
		if a.UnscopedUniqueKeys, err = pgx.CollectRows(rows, pgx.RowTo[string]); err != nil {
			return nil, MapError(err)
		}
		out = append(out, a)
	}
	return out, nil
}

// ConnectionTenantForTest 从连接池借一个连接（不设租户），返回它上面的租户会话变量；供测试核对连接归还时已清除。
func (p *Provider) ConnectionTenantForTest(ctx context.Context) (string, error) {
	var v string
	err := p.pool.QueryRow(ctx, "SELECT coalesce(current_setting($1, true), '')", TenantSetting).Scan(&v)
	return v, MapError(err)
}

// --- 事务 ---

type store struct {
	p     *Provider
	owner string
}

func (s *store) Transaction(ctx context.Context, opts relational.TxOptions, fn func(ctx context.Context, tx relational.Tx) error) error {
	if err := s.p.faults.Check("Transaction"); err != nil {
		return err
	}
	txOpts := pgx.TxOptions{}
	if opts.ReadOnly {
		txOpts.AccessMode = pgx.ReadOnly
	}
	tx, err := s.p.pool.BeginTx(ctx, txOpts)
	if err != nil {
		return MapError(err)
	}
	committed := false
	defer func() {
		if !committed {
			_ = tx.Rollback(context.Background())
		}
	}()
	// 只在本事务内有效（is_local = true）：提交或回滚后自动恢复，连接归还时不带着租户。
	if _, err := tx.Exec(ctx, "SELECT set_config($1, $2, true)", TenantSetting, s.owner); err != nil {
		return MapError(err)
	}
	if err := fn(ctx, &txn{tx: tx, faults: s.p.faults}); err != nil {
		return err
	}
	if err := tx.Commit(ctx); err != nil {
		return MapError(err)
	}
	committed = true
	return nil
}

type txn struct {
	tx     pgx.Tx
	faults *testkit.Faults
}

// prepare 拒绝碰租户的 SQL，并给 ON CONFLICT 的冲突目标补上 tenant_id（唯一约束都以它开头）。
func prepare(sql string) (string, error) {
	if forbidden.MatchString(sql) {
		return "", providererr.New(providererr.Invalid, "tenant_sql", "应用 SQL 不能碰租户列、会话变量或会话参数")
	}
	return onConflict.ReplaceAllStringFunc(sql, func(m string) string { return m + TenantColumn + ", " }), nil
}

func (t *txn) Execute(ctx context.Context, sql string, args ...any) (int64, error) {
	if err := t.faults.Check("Execute"); err != nil {
		return 0, err
	}
	q, err := prepare(sql)
	if err != nil {
		return 0, err
	}
	tag, err := t.tx.Exec(ctx, q, args...)
	if err != nil {
		return 0, MapError(err)
	}
	return tag.RowsAffected(), nil
}

func (t *txn) query(ctx context.Context, sql string, args []any) ([]relational.Row, error) {
	q, err := prepare(sql)
	if err != nil {
		return nil, err
	}
	rows, err := t.tx.Query(ctx, q, args...)
	if err != nil {
		return nil, MapError(err)
	}
	defer rows.Close()
	fields := rows.FieldDescriptions()
	var out []relational.Row
	for rows.Next() {
		values, err := rows.Values()
		if err != nil {
			return nil, MapError(err)
		}
		row := relational.Row{}
		for i, f := range fields {
			if f.Name == TenantColumn {
				continue // 应用看不到租户列（例如 SELECT *）
			}
			row[f.Name] = values[i]
		}
		out = append(out, row)
	}
	if err := rows.Err(); err != nil {
		return nil, MapError(err)
	}
	return out, nil
}

func (t *txn) Query(ctx context.Context, sql string, args ...any) ([]relational.Row, error) {
	if err := t.faults.Check("Query"); err != nil {
		return nil, err
	}
	return t.query(ctx, sql, args)
}

func (t *txn) QueryOne(ctx context.Context, sql string, args ...any) (relational.Row, bool, error) {
	if err := t.faults.Check("QueryOne"); err != nil {
		return nil, false, err
	}
	rows, err := t.query(ctx, sql, args)
	if err != nil {
		return nil, false, err
	}
	switch len(rows) {
	case 0:
		return nil, false, nil
	case 1:
		return rows[0], true, nil
	default:
		return nil, false, providererr.New(providererr.Invalid, "too_many_rows", "QueryOne 得到了多行")
	}
}
