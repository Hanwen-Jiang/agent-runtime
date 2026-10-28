// Package sqlitestore 是 SQLite RelationalStore（开发实现）。语义见 providers/relational。
//
// 每个作用域一个库文件：<Dir>/<租户 ID 或 _platform>.sqlite3，租户隔离靠库文件本身；与 Python 侧
// （agentruntime_core.dev.relational_store）布局相同。驱动是纯 Go 的 modernc.org/sqlite，不需要 C 编译器。
// 每个库只开一个连接，同一作用域的事务因此串行。SQL 里的 $1、$2 换成 SQLite 的 ?1、?2；只读事务用 PRAGMA query_only 强制。
package sqlitestore

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"

	_ "modernc.org/sqlite"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// 字符串字面量原样保留，其余位置的 $N 换成 ?N。
var token = regexp.MustCompile(`'(?:[^']|'')*'|"(?:[^"]|"")*"|\$(\d+)`)

// Translate 把 $N 占位符换成 SQLite 的 ?N。
func Translate(query string) string {
	return token.ReplaceAllStringFunc(query, func(m string) string {
		if strings.HasPrefix(m, "$") {
			return "?" + m[1:]
		}
		return m
	})
}

// MapError 把 SQLite 的错误映射到统一的错误模型。
func MapError(err error) error {
	text := strings.ToLower(err.Error())
	switch {
	case strings.Contains(text, "unique constraint") || strings.Contains(text, "primary key"):
		return providererr.Wrap(providererr.Conflict, "unique_violation", "唯一约束冲突", err)
	case strings.Contains(text, "constraint failed"):
		return providererr.Wrap(providererr.Invalid, "constraint_violation", "违反约束", err)
	case strings.Contains(text, "readonly") || strings.Contains(text, "read-only"):
		return providererr.Wrap(providererr.Invalid, "read_only", "只读事务里不能写入", err)
	case strings.Contains(text, "locked") || strings.Contains(text, "busy"):
		return providererr.Wrap(providererr.Transient, "busy", "数据库忙", err)
	default:
		return providererr.Wrap(providererr.Invalid, "bad_sql", "SQL 不合法", err)
	}
}

// Options 是构造参数。
type Options struct {
	Dir    string
	Faults *testkit.Faults
}

// Provider 是 SQLite RelationalStore 的工厂。
type Provider struct {
	mu         sync.Mutex
	opts       Options
	migrations map[int]relational.Migration
	dbs        map[string]*sql.DB
}

// New 构造一个 SQLite RelationalStore。
func New(opts Options) (*Provider, error) {
	if opts.Dir == "" {
		return nil, errors.New("sqlitestore: Dir 必填")
	}
	if err := os.MkdirAll(opts.Dir, 0o700); err != nil {
		return nil, err
	}
	return &Provider{opts: opts, migrations: map[int]relational.Migration{}, dbs: map[string]*sql.DB{}}, nil
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

func (p *Provider) Migrate(ctx context.Context, migrations []relational.Migration) error {
	p.mu.Lock()
	for _, m := range migrations {
		p.migrations[m.Version] = m
	}
	p.mu.Unlock()
	files, err := filepath.Glob(filepath.Join(p.opts.Dir, "*.sqlite3"))
	if err != nil {
		return err
	}
	for _, f := range files {
		if _, err := p.db(ctx, strings.TrimSuffix(filepath.Base(f), ".sqlite3")); err != nil {
			return err
		}
	}
	return nil
}

func (p *Provider) Close() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, db := range p.dbs {
		_ = db.Close()
	}
	p.dbs = map[string]*sql.DB{}
	return nil
}

// db 打开作用域的库，并补上尚未执行的结构变更。
func (p *Provider) db(ctx context.Context, owner string) (*sql.DB, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	db, ok := p.dbs[owner]
	if !ok {
		var err error
		// busy_timeout：开发时 task-service 与 Worker 共用这些库文件（业务库是一个，docs/plan/sandbox-platform.md 第 1 节），
		// 另一个进程正在写时等一会儿，不立即报 database is locked（Python 侧 sqlite3 默认等 5 秒）。
		db, err = sql.Open("sqlite", filepath.Join(p.opts.Dir, owner+".sqlite3")+"?_pragma=busy_timeout(5000)")
		if err != nil {
			return nil, providererr.Wrap(providererr.Transient, "open_failed", "打开数据库失败", err)
		}
		db.SetMaxOpenConns(1)
		if _, err := db.ExecContext(ctx, "CREATE TABLE IF NOT EXISTS _migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL)"); err != nil {
			return nil, MapError(err)
		}
		p.dbs[owner] = db
	}
	return db, p.migrateLocked(ctx, db)
}

func (p *Provider) migrateLocked(ctx context.Context, db *sql.DB) error {
	versions := make([]int, 0, len(p.migrations))
	for v := range p.migrations {
		versions = append(versions, v)
	}
	sort.Ints(versions)
	for _, v := range versions {
		var count int
		if err := db.QueryRowContext(ctx, "SELECT COUNT(*) FROM _migrations WHERE version = ?", v).Scan(&count); err != nil {
			return MapError(err)
		}
		if count > 0 {
			continue
		}
		m := p.migrations[v]
		tx, err := db.BeginTx(ctx, nil)
		if err != nil {
			return MapError(err)
		}
		for _, stmt := range m.Statements {
			if _, err := tx.ExecContext(ctx, stmt); err != nil {
				_ = tx.Rollback()
				return MapError(err)
			}
		}
		if _, err := tx.ExecContext(ctx, "INSERT INTO _migrations (version, name) VALUES (?, ?)", v, m.Name); err != nil {
			_ = tx.Rollback()
			return MapError(err)
		}
		if err := tx.Commit(); err != nil {
			return MapError(err)
		}
	}
	return nil
}

type store struct {
	p     *Provider
	owner string
}

func (s *store) Transaction(ctx context.Context, opts relational.TxOptions, fn func(ctx context.Context, tx relational.Tx) error) (err error) {
	if err := s.p.opts.Faults.Check("Transaction"); err != nil {
		return err
	}
	db, err := s.p.db(ctx, s.owner)
	if err != nil {
		return err
	}
	conn, err := db.Conn(ctx)
	if err != nil {
		return providererr.Wrap(providererr.Transient, "busy", "取连接失败", err)
	}
	defer conn.Close()
	if opts.ReadOnly {
		if _, err := conn.ExecContext(ctx, "PRAGMA query_only = ON"); err != nil {
			return MapError(err)
		}
		defer func() { _, _ = conn.ExecContext(context.Background(), "PRAGMA query_only = OFF") }()
	}
	begin := "BEGIN IMMEDIATE"
	if opts.ReadOnly {
		begin = "BEGIN"
	}
	if _, err := conn.ExecContext(ctx, begin); err != nil {
		return MapError(err)
	}
	committed := false
	defer func() {
		if !committed {
			_, _ = conn.ExecContext(context.Background(), "ROLLBACK")
		}
	}()
	if err := fn(ctx, &tx{conn: conn, faults: s.p.opts.Faults}); err != nil {
		return err
	}
	if _, err := conn.ExecContext(ctx, "COMMIT"); err != nil {
		return MapError(err)
	}
	committed = true
	return nil
}

type tx struct {
	conn   *sql.Conn
	faults *testkit.Faults
}

func (t *tx) Execute(ctx context.Context, query string, args ...any) (int64, error) {
	if err := t.faults.Check("Execute"); err != nil {
		return 0, err
	}
	res, err := t.conn.ExecContext(ctx, Translate(query), args...)
	if err != nil {
		return 0, MapError(err)
	}
	n, _ := res.RowsAffected()
	return n, nil
}

func (t *tx) query(ctx context.Context, query string, args []any) ([]relational.Row, error) {
	rows, err := t.conn.QueryContext(ctx, Translate(query), args...)
	if err != nil {
		return nil, MapError(err)
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		return nil, MapError(err)
	}
	var out []relational.Row
	for rows.Next() {
		values := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range values {
			ptrs[i] = &values[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, MapError(err)
		}
		row := relational.Row{}
		for i, c := range cols {
			if b, ok := values[i].([]byte); ok {
				values[i] = string(b)
			}
			row[c] = values[i]
		}
		out = append(out, row)
	}
	if err := rows.Err(); err != nil {
		return nil, MapError(err)
	}
	return out, nil
}

func (t *tx) Query(ctx context.Context, query string, args ...any) ([]relational.Row, error) {
	if err := t.faults.Check("Query"); err != nil {
		return nil, err
	}
	return t.query(ctx, query, args)
}

func (t *tx) QueryOne(ctx context.Context, query string, args ...any) (relational.Row, bool, error) {
	if err := t.faults.Check("QueryOne"); err != nil {
		return nil, false, err
	}
	rows, err := t.query(ctx, query, args)
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
