// Package ledger 是沙箱环境表（设计书改动稿 W9；D-11）。SBX-01 起表与迁移归 Sandbox Service（从 Worker 的
// agentruntime_worker.sandbox.ledger 搬来，表结构、迁移的版本号与语句、时间的写法都不变：上线前 Worker 记的行照常可用）。
//
//   - 表在业务库（RelationalStore，按租户行级安全；开发时是 SQLite），迁移版本 201 起。
//   - 状态：creating → ready → destroying → destroyed；没建成是 failed，后端里没了是 lost。调后端之前先落账，之后补状态；
//     任何一步中断留下的状态由对账修，不靠进程内的记忆。
//   - 建环境时在同一个事务里核对配额与预算（W10）：先更新本租户 sandbox_quota_lock 的那一行（Postgres 上是行锁），
//     并发建环境在这一步串行。超了 denied（sandbox_quota_exceeded、sandbox_budget_exceeded）。
//   - 平台作用域的 sandbox_env_directory 记着每个环境属于哪个作用域，只给对账用（先写目录再写租户的行）。
//   - 时间一律是 ISO 8601 UTC 文本（微秒，可以按字典序比较）；布尔是 0/1。
package ledger

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"math"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
)

// Purpose 是平台作用域的用途。
const Purpose = "sandbox"

// PlatformID 是平台作用域的 owner。
const PlatformID = devutil.PlatformID

// TimeLayout 与 Python 的 iso() 相同：微秒、Z 结尾。
const TimeLayout = "2006-01-02T15:04:05.000000Z"

// ISO 把时间写成环境表里的文本。
func ISO(t time.Time) string { return t.UTC().Format(TimeLayout) }

// ParseISO 解析环境表里的时间；空串或格式不对时零值。
func ParseISO(s string) time.Time {
	if s == "" {
		return time.Time{}
	}
	t, err := time.Parse(TimeLayout, s)
	if err != nil {
		return time.Time{}
	}
	return t
}

// Migrations 是环境表的迁移（与原来 Python 的 MIGRATIONS 逐字相同）。
var Migrations = []relational.Migration{
	{Version: 201, Name: "sandbox_envs", Statements: []string{
		`CREATE TABLE sandbox_envs (
            env_ref TEXT PRIMARY KEY,
            backend TEXT NOT NULL,
            kind TEXT NOT NULL,
            isolation TEXT NOT NULL,
            backend_env_id TEXT NOT NULL DEFAULT '',
            workspace_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            writable BIGINT NOT NULL DEFAULT 0,
            runtime TEXT NOT NULL DEFAULT '',
            image TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            ready_at TEXT NOT NULL DEFAULT '',
            last_used_at TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            max_until TEXT NOT NULL DEFAULT '',
            destroyed_at TEXT NOT NULL DEFAULT '',
            idle_seconds BIGINT NOT NULL DEFAULT 0,
            busy_until TEXT NOT NULL DEFAULT '',
            cpu_ms BIGINT NOT NULL DEFAULT 0,
            alive_ms BIGINT NOT NULL DEFAULT 0,
            cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
            fallback_from TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT ''
        )`,
		"CREATE INDEX sandbox_envs_session ON sandbox_envs (workspace_id, session_id, state)",
		"CREATE INDEX sandbox_envs_backend ON sandbox_envs (backend, backend_env_id)",
		"CREATE INDEX sandbox_envs_state ON sandbox_envs (state)",
		`CREATE TABLE sandbox_usage (
            day TEXT PRIMARY KEY,
            sandbox_seconds BIGINT NOT NULL DEFAULT 0,
            egress_bytes BIGINT NOT NULL DEFAULT 0,
            mirror_miss_bytes BIGINT NOT NULL DEFAULT 0,
            cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0
        )`,
		"CREATE TABLE sandbox_quota_lock (id TEXT PRIMARY KEY, version BIGINT NOT NULL DEFAULT 0)",
		`CREATE TABLE sandbox_env_directory (
            env_ref TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            backend TEXT NOT NULL,
            state TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )`,
		"CREATE INDEX sandbox_env_directory_state ON sandbox_env_directory (state)",
	}},
	{Version: 202, Name: "sandbox_grants", Statements: []string{
		`CREATE TABLE sandbox_grants (
            grant_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            hosts TEXT NOT NULL,
            ports TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            max_up_bytes BIGINT NOT NULL DEFAULT 0,
            max_down_bytes BIGINT NOT NULL DEFAULT 0,
            max_conns BIGINT NOT NULL DEFAULT 0,
            max_rps BIGINT NOT NULL DEFAULT 0,
            up_bytes BIGINT NOT NULL DEFAULT 0,
            down_bytes BIGINT NOT NULL DEFAULT 0,
            preview_port BIGINT NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            approved_by TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            revoked_at TEXT NOT NULL DEFAULT ''
        )`,
		"CREATE INDEX sandbox_grants_session ON sandbox_grants (session_id, state)",
		"CREATE INDEX sandbox_grants_task ON sandbox_grants (task_id, state)",
	}},
	{Version: 203, Name: "sandbox_egress", Statements: []string{
		"ALTER TABLE sandbox_grants ADD COLUMN preview_env TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE sandbox_grants ADD COLUMN preview_target TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE sandbox_envs ADD COLUMN allow_hosts TEXT NOT NULL DEFAULT ''",
		`CREATE TABLE sandbox_egress_audit (
            audit_id TEXT PRIMARY KEY,
            grant_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            env_ref TEXT NOT NULL,
            direction TEXT NOT NULL,
            host TEXT NOT NULL,
            port BIGINT NOT NULL DEFAULT 0,
            address TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            duration_ms BIGINT NOT NULL DEFAULT 0,
            up_bytes BIGINT NOT NULL DEFAULT 0,
            down_bytes BIGINT NOT NULL DEFAULT 0,
            result TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT ''
        )`,
		"CREATE INDEX sandbox_egress_audit_task ON sandbox_egress_audit (task_id, started_at)",
	}},
	{Version: 204, Name: "sandbox_images", Statements: []string{
		`CREATE TABLE sandbox_images (
            image_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '',
            dockerfile TEXT NOT NULL,
            base_image TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            image TEXT NOT NULL DEFAULT '',
            digest TEXT NOT NULL DEFAULT '',
            size_bytes BIGINT NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            ready_at TEXT NOT NULL DEFAULT '',
            last_used_at TEXT NOT NULL DEFAULT '',
            build_ms BIGINT NOT NULL DEFAULT 0,
            deleted_at TEXT NOT NULL DEFAULT ''
        )`,
		"CREATE INDEX sandbox_images_state ON sandbox_images (state, last_used_at)",
	}},
	{Version: 206, Name: "sandbox_environment_status", Statements: []string{
		"ALTER TABLE sandbox_envs ADD COLUMN health_confirmed_at TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE sandbox_envs ADD COLUMN reused_at TEXT NOT NULL DEFAULT ''",
	}},
	{Version: 207, Name: "sandbox_environment_setup", Statements: []string{
		"ALTER TABLE sandbox_envs ADD COLUMN environment_id TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE sandbox_envs ADD COLUMN setup_state TEXT NOT NULL DEFAULT 'none'",
		"ALTER TABLE sandbox_envs ADD COLUMN setup_key TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE sandbox_envs ADD COLUMN setup_finished_at TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE sandbox_envs ADD COLUMN setup_error TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE sandbox_envs ADD COLUMN start_state TEXT NOT NULL DEFAULT 'none'",
	}},
	{Version: 211, Name: "sandbox_grant_scope", Statements: []string{
		"ALTER TABLE sandbox_grants ADD COLUMN scope TEXT NOT NULL DEFAULT 'hosts'",
	}},
	{Version: 218, Name: "sandbox_archive_lease", Statements: []string{
		"ALTER TABLE sandbox_envs ADD COLUMN archive_token TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE sandbox_envs ADD COLUMN archive_until TEXT NOT NULL DEFAULT ''",
	}},
}

// Record 是环境表的一行。
type Record struct {
	EnvRef            string
	Owner             string
	Backend           string
	Kind              string
	Isolation         string
	State             string
	CreatedAt         string
	BackendEnvID      string
	WorkspaceID       string
	SessionID         string
	TaskID            string
	Writable          bool
	Runtime           string
	Image             string
	ReadyAt           string
	LastUsedAt        string
	ExpiresAt         string
	MaxUntil          string
	DestroyedAt       string
	IdleSeconds       int64
	ArchiveToken      string
	ArchiveUntil      string
	BusyUntil         string
	CPUMS             int64
	AliveMS           int64
	CostUSD           float64
	FallbackFrom      string
	Reason            string
	AllowHosts        string
	HealthConfirmedAt string
	ReusedAt          string
	EnvironmentID     string
	SetupState        string
	SetupKey          string
	SetupFinishedAt   string
	SetupError        string
	StartState        string
}

// Columns 是 SELECT 与 INSERT 用的列（不含 owner：owner 是作用域，存储负责）。
var Columns = []string{
	"env_ref", "backend", "kind", "isolation", "state", "created_at", "backend_env_id", "workspace_id", "session_id", "task_id", "writable",
	"runtime", "image", "ready_at", "last_used_at", "expires_at", "max_until", "destroyed_at", "idle_seconds", "busy_until", "cpu_ms",
	"alive_ms", "cost_usd", "fallback_from", "reason", "allow_hosts", "health_confirmed_at", "reused_at", "environment_id", "setup_state",
	"setup_key", "setup_finished_at", "setup_error", "start_state", "archive_token", "archive_until",
}

var columnList = strings.Join(Columns, ", ")

func (r Record) values() []any {
	w := int64(0)
	if r.Writable {
		w = 1
	}
	return []any{r.EnvRef, r.Backend, r.Kind, r.Isolation, r.State, r.CreatedAt, r.BackendEnvID, r.WorkspaceID, r.SessionID, r.TaskID, w,
		r.Runtime, r.Image, r.ReadyAt, r.LastUsedAt, r.ExpiresAt, r.MaxUntil, r.DestroyedAt, r.IdleSeconds, r.BusyUntil, r.CPUMS,
		r.AliveMS, r.CostUSD, r.FallbackFrom, r.Reason, r.AllowHosts, r.HealthConfirmedAt, r.ReusedAt, r.EnvironmentID, r.SetupState,
		r.SetupKey, r.SetupFinishedAt, r.SetupError, r.StartState, r.ArchiveToken, r.ArchiveUntil}
}

// Str 取一列的文本。
func Str(row relational.Row, key string) string {
	switch v := row[key].(type) {
	case string:
		return v
	case []byte:
		return string(v)
	case nil:
		return ""
	default:
		return fmt.Sprint(v)
	}
}

// Int 取一列的整数。
func Int(row relational.Row, key string) int64 {
	switch v := row[key].(type) {
	case int64:
		return v
	case int32:
		return int64(v)
	case int:
		return int64(v)
	case float64:
		return int64(v)
	case string:
		var n int64
		_, _ = fmt.Sscan(v, &n)
		return n
	}
	return 0
}

// Float 取一列的浮点数。
func Float(row relational.Row, key string) float64 {
	switch v := row[key].(type) {
	case float64:
		return v
	case float32:
		return float64(v)
	case int64:
		return float64(v)
	case string:
		var f float64
		_, _ = fmt.Sscan(v, &f)
		return f
	}
	return 0
}

// FromRow 把一行变成 Record。
func FromRow(owner string, row relational.Row) Record {
	return Record{
		EnvRef: Str(row, "env_ref"), Owner: owner, Backend: Str(row, "backend"), Kind: Str(row, "kind"), Isolation: Str(row, "isolation"),
		State: Str(row, "state"), CreatedAt: Str(row, "created_at"), BackendEnvID: Str(row, "backend_env_id"), WorkspaceID: Str(row, "workspace_id"),
		SessionID: Str(row, "session_id"), TaskID: Str(row, "task_id"), Writable: Int(row, "writable") != 0, Runtime: Str(row, "runtime"),
		Image: Str(row, "image"), ReadyAt: Str(row, "ready_at"), LastUsedAt: Str(row, "last_used_at"), ExpiresAt: Str(row, "expires_at"),
		MaxUntil: Str(row, "max_until"), DestroyedAt: Str(row, "destroyed_at"), IdleSeconds: Int(row, "idle_seconds"), BusyUntil: Str(row, "busy_until"), ArchiveToken: Str(row, "archive_token"), ArchiveUntil: Str(row, "archive_until"),
		CPUMS: Int(row, "cpu_ms"), AliveMS: Int(row, "alive_ms"), CostUSD: Float(row, "cost_usd"), FallbackFrom: Str(row, "fallback_from"),
		Reason: Str(row, "reason"), AllowHosts: Str(row, "allow_hosts"), HealthConfirmedAt: Str(row, "health_confirmed_at"), ReusedAt: Str(row, "reused_at"),
		EnvironmentID: Str(row, "environment_id"), SetupState: Str(row, "setup_state"), SetupKey: Str(row, "setup_key"),
		SetupFinishedAt: Str(row, "setup_finished_at"), SetupError: Str(row, "setup_error"), StartState: Str(row, "start_state"),
	}
}

// Expired 判断空闲到期或活满。
func (r Record) Expired(now time.Time) bool {
	n := ISO(now)
	return (r.ExpiresAt != "" && r.ExpiresAt <= n) || (r.MaxUntil != "" && r.MaxUntil <= n)
}

// Ledger 是环境表的读写。
type Ledger struct {
	store    relational.Provider
	clock    clock.Clock
	mu       sync.Mutex
	migrated bool
}

// New 构造。
func New(store relational.Provider, c clock.Clock) *Ledger {
	if c == nil {
		c = clock.System{}
	}
	return &Ledger{store: store, clock: c}
}

// Now 是环境表用的时钟。
func (l *Ledger) Now() time.Time { return l.clock.Now().UTC() }

// Migrate 建表（幂等）。
func (l *Ledger) Migrate(ctx context.Context) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.migrated {
		return nil
	}
	if err := l.store.Migrate(ctx, Migrations); err != nil {
		return err
	}
	if err := l.store.Migrate(ctx, TransferUsageMigrations); err != nil {
		return err
	}
	l.migrated = true
	return nil
}

// Bind 是这个作用域的业务库（租户条件由存储负责，SQL 里不写）。
func (l *Ledger) Bind(owner string) (relational.Store, error) {
	if owner == PlatformID {
		return l.store.BindPlatform(Purpose)
	}
	return l.store.Bind(&commonv1.TenantContext{TenantId: owner})
}

// Tx 在这个作用域里开一个事务。
func (l *Ledger) Tx(ctx context.Context, owner string, readOnly bool, fn func(ctx context.Context, tx relational.Tx) error) error {
	if err := l.Migrate(ctx); err != nil {
		return err
	}
	store, err := l.Bind(owner)
	if err != nil {
		return err
	}
	return store.Transaction(ctx, relational.TxOptions{ReadOnly: readOnly}, fn)
}

func newEnvRef() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return "sbe-" + hex.EncodeToString(b)
}

// CreateArgs 是 BeginCreate 的参数。
type CreateArgs struct {
	Backend, Kind, Isolation       string
	WorkspaceID, SessionID, TaskID string
	Writable                       bool
	Runtime, Image                 string
	Idle, MaxLifetime              time.Duration
	Quota                          *registry.Quota
	FallbackFrom, AllowHosts       string
	// Exclusive（存档模式的工作区，D-06）：这个工作区不管哪个会话有可写的环境都算占用。
	Exclusive                 bool
	EnvironmentID, SetupState string
}

// BeginCreate 落一行 creating（先写平台目录再写租户的行），同一个事务里核对配额与预算。超了 denied。
func (l *Ledger) BeginCreate(ctx context.Context, owner string, a CreateArgs) (Record, error) {
	if err := l.Migrate(ctx); err != nil {
		return Record{}, err
	}
	now := l.Now()
	setup := a.SetupState
	if setup == "" {
		setup = "none"
	}
	rec := Record{
		EnvRef: newEnvRef(), Owner: owner, Backend: a.Backend, Kind: a.Kind, Isolation: a.Isolation, State: "creating", CreatedAt: ISO(now),
		WorkspaceID: a.WorkspaceID, SessionID: a.SessionID, TaskID: a.TaskID, Writable: a.Writable, Runtime: a.Runtime, Image: a.Image,
		IdleSeconds: int64(a.Idle.Seconds()), MaxUntil: ISO(now.Add(a.MaxLifetime)), FallbackFrom: a.FallbackFrom, AllowHosts: a.AllowHosts,
		EnvironmentID: a.EnvironmentID, SetupState: setup, StartState: "none",
	}
	if err := l.directory(ctx, rec, "creating"); err != nil {
		return Record{}, err
	}
	if err := l.insertChecked(ctx, rec, a.Quota, now, a.Exclusive); err != nil {
		_ = l.directory(context.WithoutCancel(ctx), rec, "failed")
		return Record{}, err
	}
	return rec, nil
}

func (l *Ledger) insertChecked(ctx context.Context, rec Record, quota *registry.Quota, now time.Time, exclusive bool) error {
	placeholders := make([]string, len(Columns))
	for i := range Columns {
		placeholders[i] = fmt.Sprintf("$%d", i+1)
	}
	return l.Tx(ctx, rec.Owner, false, func(ctx context.Context, tx relational.Tx) error {
		if _, err := tx.Execute(ctx, "INSERT INTO sandbox_quota_lock (id, version) VALUES ('envs', 1) ON CONFLICT (id) DO UPDATE SET version = sandbox_quota_lock.version + 1"); err != nil {
			return err
		}
		if quota != nil {
			if err := checkQuota(ctx, tx, *quota, now); err != nil {
				return err
			}
		}
		if rec.Writable && rec.WorkspaceID != "" {
			// 一个工作区同一时间只有一个可写的环境：挂卷时同一个会话的几个环境看到的是同一处，只拦别的会话；存档模式谁都拦。
			var row relational.Row
			var err error
			if exclusive {
				row, _, err = tx.QueryOne(ctx, "SELECT COUNT(*) AS n FROM sandbox_envs WHERE workspace_id = $1 AND writable = 1 AND state IN ('creating', 'ready')", rec.WorkspaceID)
			} else {
				row, _, err = tx.QueryOne(ctx, "SELECT COUNT(*) AS n FROM sandbox_envs WHERE workspace_id = $1 AND session_id <> $2 AND writable = 1 AND state IN ('creating', 'ready')", rec.WorkspaceID, rec.SessionID)
			}
			if err != nil {
				return err
			}
			if Int(row, "n") > 0 {
				return providererr.New(providererr.Denied, "workspace_busy", "这个工作区正被另一个会话的可写环境占用，等它结束或回收")
			}
		}
		_, err := tx.Execute(ctx, "INSERT INTO sandbox_envs ("+columnList+") VALUES ("+strings.Join(placeholders, ", ")+")", rec.values()...)
		return err
	})
}

func checkQuota(ctx context.Context, tx relational.Tx, q registry.Quota, now time.Time) error {
	active, err := tx.Query(ctx, "SELECT created_at, state FROM sandbox_envs WHERE state IN ('creating', 'ready', 'destroying')")
	if err != nil {
		return err
	}
	if q.MaxConcurrent > 0 && len(active) >= q.MaxConcurrent {
		return providererr.New(providererr.Denied, "sandbox_quota_exceeded", fmt.Sprintf("同时最多 %d 个沙箱环境，先等前面的结束", q.MaxConcurrent))
	}
	today := now.Format("2006-01-02")
	usage, _, err := tx.QueryOne(ctx, "SELECT sandbox_seconds, egress_bytes, mirror_miss_bytes, cost_usd FROM sandbox_usage WHERE day = $1", today)
	if err != nil {
		return err
	}
	used := Int(usage, "sandbox_seconds")
	midnight := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
	for _, row := range active {
		started := ParseISO(Str(row, "created_at"))
		if started.IsZero() {
			started = now
		}
		if started.Before(midnight) {
			started = midnight
		}
		used += int64(now.Sub(started).Seconds())
	}
	if q.DailySandboxSeconds > 0 && used >= q.DailySandboxSeconds {
		return providererr.New(providererr.Denied, "sandbox_quota_exceeded", fmt.Sprintf("今天的沙箱时长到了上限（%d 秒）", q.DailySandboxSeconds))
	}
	if q.DailyEgressBytes > 0 && Int(usage, "egress_bytes") >= q.DailyEgressBytes {
		return providererr.New(providererr.Denied, "sandbox_quota_exceeded", "今天的出网流量到了上限")
	}
	if q.DailyMirrorMissBytes > 0 && Int(usage, "mirror_miss_bytes") >= q.DailyMirrorMissBytes {
		return providererr.New(providererr.Denied, "sandbox_quota_exceeded", "今天从上游拉依赖的流量到了上限")
	}
	if q.DailyCostUSD > 0 && Float(usage, "cost_usd") >= q.DailyCostUSD {
		return providererr.New(providererr.Denied, "sandbox_budget_exceeded", fmt.Sprintf("今天的沙箱花费到了上限（%g 美元）", q.DailyCostUSD))
	}
	if q.MonthlyCostUSD > 0 {
		month, _, err := tx.QueryOne(ctx, "SELECT SUM(cost_usd) AS c FROM sandbox_usage WHERE day >= $1 AND day <= $2", today[:8]+"01", today)
		if err != nil {
			return err
		}
		if Float(month, "c") >= q.MonthlyCostUSD {
			return providererr.New(providererr.Denied, "sandbox_budget_exceeded", fmt.Sprintf("本月的沙箱花费到了上限（%g 美元）", q.MonthlyCostUSD))
		}
	}
	return nil
}

func minTime(a, b time.Time) time.Time {
	if b.IsZero() || a.Before(b) {
		return a
	}
	return b
}

// MarkReady 记就绪；image 是后端报的实际镜像（按 digest），报了就以它为准。
func (l *Ledger) MarkReady(ctx context.Context, rec Record, backendEnvID, image string, healthConfirmed bool) (Record, error) {
	now := l.Now()
	rec.State, rec.BackendEnvID, rec.ReadyAt, rec.LastUsedAt = "ready", backendEnvID, ISO(now), ISO(now)
	rec.ExpiresAt = ISO(minTime(now.Add(time.Duration(rec.IdleSeconds)*time.Second), ParseISO(rec.MaxUntil)))
	if image != "" {
		rec.Image = image
	}
	if healthConfirmed {
		rec.HealthConfirmedAt = ISO(now)
	}
	if err := l.update(ctx, rec, "state", "backend_env_id", "ready_at", "last_used_at", "expires_at", "image", "health_confirmed_at"); err != nil {
		return rec, err
	}
	return rec, l.directory(ctx, rec, "ready")
}

// MarkSetup 记 setup、start 的结果（ENV-02），只改给出的列。
func (l *Ledger) MarkSetup(ctx context.Context, rec Record, facts map[string]string) (Record, error) {
	var cols []string
	for _, c := range []string{"environment_id", "setup_state", "setup_key", "setup_finished_at", "setup_error", "start_state"} {
		v, ok := facts[c]
		if !ok {
			continue
		}
		cols = append(cols, c)
		switch c {
		case "environment_id":
			rec.EnvironmentID = v
		case "setup_state":
			rec.SetupState = v
		case "setup_key":
			rec.SetupKey = v
		case "setup_finished_at":
			rec.SetupFinishedAt = v
		case "setup_error":
			rec.SetupError = v
		case "start_state":
			rec.StartState = v
		}
	}
	if len(cols) == 0 {
		return rec, nil
	}
	return rec, l.update(ctx, rec, cols...)
}

func cut(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// MarkFailed 记没建成。
func (l *Ledger) MarkFailed(ctx context.Context, rec Record, reason string) (Record, error) {
	rec.State, rec.Reason, rec.DestroyedAt = "failed", cut(reason, 300), ISO(l.Now())
	if err := l.update(ctx, rec, "state", "reason", "destroyed_at"); err != nil {
		return rec, err
	}
	return rec, l.directory(ctx, rec, "failed")
}

func (l *Ledger) query(ctx context.Context, owner, sql string, args ...any) ([]Record, error) {
	var out []Record
	err := l.Tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, sql, args...)
		for _, row := range rows {
			out = append(out, FromRow(owner, row))
		}
		return err
	})
	return out, err
}

// FindReusable 是同一个（工作区，会话）里未活满的 ready 环境（W5），最近用过的先给。
// 空闲到期只是 GC 的候选条件；只要尚未被回收，同会话仍可经健康检查和 CAS 续租，避免满配额时新建失败。
func (l *Ledger) FindReusable(ctx context.Context, owner, workspaceID, sessionID string, backends []string, writable bool) (*Record, error) {
	now := ISO(l.Now())
	w := 0
	if writable {
		w = 1
	}
	recs, err := l.query(ctx, owner, "SELECT "+columnList+" FROM sandbox_envs WHERE workspace_id = $1 AND session_id = $2 AND state = 'ready' "+
		"AND writable = $3 AND max_until > $4 AND (archive_until = '' OR archive_until <= $4) ORDER BY last_used_at DESC", workspaceID, sessionID, w, now)
	if err != nil {
		return nil, err
	}
	for _, r := range recs {
		for _, b := range backends {
			if r.Backend == b {
				r := r
				return &r, nil
			}
		}
	}
	return nil, nil
}

// TouchArgs 是 Touch 的参数。
type TouchArgs struct {
	// ArchiveToken is supplied only by the operation which holds an archive claim.
	ArchiveToken string
	CPUMS        int64
	TaskID       string
	// BusyFor 给了时记下命令最晚跑到什么时候（这期间别的会话不能回收它），没给时清掉。
	BusyFor         *time.Duration
	HealthConfirmed *bool
	Reused          bool
}

// Touch 用了一次：顺延空闲到期（不超过活满），累加 CPU 时间。
func (l *Ledger) Touch(ctx context.Context, rec Record, a TouchArgs) (Record, error) {
	now := l.Now()
	expires := minTime(now.Add(time.Duration(rec.IdleSeconds)*time.Second), ParseISO(rec.MaxUntil))
	busy := ""
	if a.BusyFor != nil {
		busy = ISO(now.Add(*a.BusyFor))
	}
	task := a.TaskID
	if task == "" {
		task = rec.TaskID
	}
	values := []any{ISO(now), ISO(expires), a.CPUMS, task, busy}
	extra := ""
	if a.HealthConfirmed != nil {
		v := ""
		if *a.HealthConfirmed {
			v = ISO(now)
		}
		values = append(values, v)
		extra += fmt.Sprintf(", health_confirmed_at = $%d", len(values))
		rec.HealthConfirmedAt = v
	}
	if a.Reused && rec.WorkspaceID != "" && rec.SessionID != "" {
		values = append(values, ISO(now))
		extra += fmt.Sprintf(", reused_at = $%d", len(values))
		rec.ReusedAt = ISO(now)
	}
	values = append(values, rec.EnvRef, ISO(now), a.ArchiveToken)
	envIndex, nowIndex, tokenIndex := len(values)-2, len(values)-1, len(values)
	err := l.Tx(ctx, rec.Owner, false, func(ctx context.Context, tx relational.Tx) error {
		n, err := tx.Execute(ctx, "UPDATE sandbox_envs SET last_used_at = $1, expires_at = $2, cpu_ms = cpu_ms + $3, task_id = $4, busy_until = $5"+
			extra+fmt.Sprintf(" WHERE env_ref = $%d AND state = 'ready' AND (archive_until = '' OR archive_until <= $%d OR (archive_token = $%d AND archive_token <> ''))", envIndex, nowIndex, tokenIndex), values...)
		if err != nil {
			return err
		}
		if n != 1 {
			return providererr.New(providererr.Conflict, "workspace_busy", "环境正在归档或借用已经结束")
		}
		return nil
	})
	rec.LastUsedAt, rec.ExpiresAt, rec.CPUMS, rec.TaskID, rec.BusyUntil = ISO(now), ISO(expires), rec.CPUMS+a.CPUMS, task, busy
	return rec, err
}

// WritableHolders 是占着这个工作区的可写环境（creating 或 ready）；sessionID 为 nil 时不分会话（存档模式），否则只算别的会话。
func (l *Ledger) WritableHolders(ctx context.Context, owner, workspaceID string, sessionID *string) ([]Record, error) {
	if sessionID == nil {
		return l.query(ctx, owner, "SELECT "+columnList+" FROM sandbox_envs WHERE workspace_id = $1 AND writable = 1 AND state IN ('creating', 'ready')", workspaceID)
	}
	return l.query(ctx, owner, "SELECT "+columnList+" FROM sandbox_envs WHERE workspace_id = $1 AND session_id <> $2 AND writable = 1 AND state IN ('creating', 'ready')", workspaceID, *sessionID)
}

// TaskRunning：环境最近一次服务的 Task 还没结束（task-service 的 tasks 表还没有结局；N-05）。表不在或读失败时当作已结束。
func (l *Ledger) TaskRunning(ctx context.Context, owner, taskID string) bool {
	if taskID == "" || owner == PlatformID {
		return false
	}
	running := false
	_ = l.Tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, "SELECT outcome FROM tasks WHERE task_id = $1", taskID)
		running = err == nil && ok && Str(row, "outcome") == ""
		return nil
	})
	return running
}

// Hold 把空闲到期顺延到 until（等人期间保留环境，N-05）。
func (l *Ledger) Hold(ctx context.Context, rec Record, until time.Time) (Record, error) {
	rec.ExpiresAt = ISO(until)
	return rec, l.update(ctx, rec, "expires_at")
}

// IdleNow：没有命令在跑（busy_until 空或已过），可以回收。
func (l *Ledger) IdleNow(rec Record) bool {
	return rec.State == "ready" && (rec.BusyUntil == "" || rec.BusyUntil <= ISO(l.Now())) && (rec.ArchiveUntil == "" || rec.ArchiveUntil <= ISO(l.Now()))
}

// BeginDestroy 记 destroying。
func (l *Ledger) BeginDestroy(ctx context.Context, rec Record, reason string) (Record, error) {
	rec.State = "destroying"
	if reason != "" {
		rec.Reason = cut(reason, 300)
	}
	if err := l.update(ctx, rec, "state", "reason"); err != nil {
		return rec, err
	}
	return rec, l.directory(ctx, rec, "destroying")
}

// MarkDestroyed 记销毁完成：存活时间从建起算，花费按后端的价目；计入当天的用量。
func (l *Ledger) MarkDestroyed(ctx context.Context, rec Record, perHour, perCPUHour float64) (Record, error) {
	now := l.Now()
	started := ParseISO(rec.CreatedAt)
	if started.IsZero() {
		started = now
	}
	alive := max(int64(0), now.Sub(started).Milliseconds())
	cost := float64(alive)/3_600_000*perHour + float64(rec.CPUMS)/3_600_000*perCPUHour
	rec.State, rec.DestroyedAt, rec.AliveMS, rec.CostUSD = "destroyed", ISO(now), alive, math.Round(cost*1e6)/1e6
	if err := l.update(ctx, rec, "state", "destroyed_at", "alive_ms", "cost_usd"); err != nil {
		return rec, err
	}
	if err := l.AddUsage(ctx, rec.Owner, now, alive/1000, 0, 0, rec.CostUSD); err != nil {
		return rec, err
	}
	return rec, l.directory(ctx, rec, "destroyed")
}

// MarkLost 记后端里没了。
func (l *Ledger) MarkLost(ctx context.Context, rec Record, reason string) (Record, error) {
	rec.State, rec.Reason, rec.DestroyedAt = "lost", cut(reason, 300), ISO(l.Now())
	if err := l.update(ctx, rec, "state", "reason", "destroyed_at"); err != nil {
		return rec, err
	}
	return rec, l.directory(ctx, rec, "lost")
}

// Get 按 env_ref 取一行；没有时 nil。
func (l *Ledger) Get(ctx context.Context, owner, envRef string) (*Record, error) {
	recs, err := l.query(ctx, owner, "SELECT "+columnList+" FROM sandbox_envs WHERE env_ref = $1", envRef)
	if err != nil || len(recs) == 0 {
		return nil, err
	}
	return &recs[0], nil
}

// ByBackendEnv 按后端给的环境 id 找；backend 为空时不限后端。
func (l *Ledger) ByBackendEnv(ctx context.Context, owner, backend, backendEnvID string) (*Record, error) {
	var recs []Record
	var err error
	if backend == "" {
		recs, err = l.query(ctx, owner, "SELECT "+columnList+" FROM sandbox_envs WHERE backend_env_id = $1 ORDER BY created_at DESC", backendEnvID)
	} else {
		recs, err = l.query(ctx, owner, "SELECT "+columnList+" FROM sandbox_envs WHERE backend = $1 AND backend_env_id = $2 ORDER BY created_at DESC", backend, backendEnvID)
	}
	if err != nil || len(recs) == 0 {
		return nil, err
	}
	return &recs[0], nil
}

// WorkspaceEnvs 是还没结束的、绑着这个工作区的环境。
func (l *Ledger) WorkspaceEnvs(ctx context.Context, owner, workspaceID string) ([]Record, error) {
	return l.query(ctx, owner, "SELECT "+columnList+" FROM sandbox_envs WHERE workspace_id = $1 AND state IN ('creating', 'ready', 'destroying')", workspaceID)
}

// WorkspaceLastUsed 是这个工作区的环境最后一次用的时间（建、就绪、用过都算）；没有时空串。
func (l *Ledger) WorkspaceLastUsed(ctx context.Context, owner, workspaceID string) (string, error) {
	out := ""
	err := l.Tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		row, _, err := tx.QueryOne(ctx, "SELECT MAX(last_used_at) AS used, MAX(ready_at) AS ready, MAX(created_at) AS created FROM sandbox_envs WHERE workspace_id = $1", workspaceID)
		for _, k := range []string{"used", "ready", "created"} {
			if v := Str(row, k); v > out {
				out = v
			}
		}
		return err
	})
	return out, err
}

// Active 是这个作用域没结束的环境。
func (l *Ledger) Active(ctx context.Context, owner string) ([]Record, error) {
	return l.query(ctx, owner, "SELECT "+columnList+" FROM sandbox_envs WHERE state IN ('creating', 'ready', 'destroying') ORDER BY created_at")
}

// DirectoryEntry 是平台目录的一行。
type DirectoryEntry struct {
	EnvRef, Owner, Backend, State, UpdatedAt string
}

// Directory 是平台目录（对账用）。
func (l *Ledger) Directory(ctx context.Context, activeOnly bool) ([]DirectoryEntry, error) {
	sql := "SELECT env_ref, owner, backend, state, updated_at FROM sandbox_env_directory"
	if activeOnly {
		sql += " WHERE state IN ('creating', 'ready', 'destroying')"
	}
	var out []DirectoryEntry
	err := l.Tx(ctx, PlatformID, true, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, sql+" ORDER BY updated_at")
		for _, r := range rows {
			out = append(out, DirectoryEntry{Str(r, "env_ref"), Str(r, "owner"), Str(r, "backend"), Str(r, "state"), Str(r, "updated_at")})
		}
		return err
	})
	return out, err
}

// SetDirectory 改平台目录里一个环境的状态（对账收拾目录里有、租户的行没有的）。
func (l *Ledger) SetDirectory(ctx context.Context, envRef, owner, backend, state string) error {
	return l.directory(ctx, Record{EnvRef: envRef, Owner: owner, Backend: backend}, state)
}

// AddUsage 累加当天的用量（平台作用域的环境不计入任何租户）。
func (l *Ledger) AddUsage(ctx context.Context, owner string, when time.Time, seconds, egress, mirrorMiss int64, cost float64) error {
	if owner == PlatformID {
		return nil
	}
	return l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, "INSERT INTO sandbox_usage (day, sandbox_seconds, egress_bytes, mirror_miss_bytes, cost_usd) VALUES ($1, $2, $3, $4, $5) "+
			"ON CONFLICT (day) DO UPDATE SET sandbox_seconds = sandbox_usage.sandbox_seconds + excluded.sandbox_seconds, "+
			"egress_bytes = sandbox_usage.egress_bytes + excluded.egress_bytes, "+
			"mirror_miss_bytes = sandbox_usage.mirror_miss_bytes + excluded.mirror_miss_bytes, "+
			"cost_usd = sandbox_usage.cost_usd + excluded.cost_usd",
			when.UTC().Format("2006-01-02"), seconds, egress, mirrorMiss, cost)
		return err
	})
}

func (l *Ledger) update(ctx context.Context, rec Record, columns ...string) error {
	all := map[string]any{}
	vals := rec.values()
	for i, c := range Columns {
		all[c] = vals[i]
	}
	sets := make([]string, len(columns))
	args := make([]any, 0, len(columns)+1)
	for i, c := range columns {
		sets[i] = fmt.Sprintf("%s = $%d", c, i+1)
		args = append(args, all[c])
	}
	args = append(args, rec.EnvRef)
	return l.Tx(ctx, rec.Owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, "UPDATE sandbox_envs SET "+strings.Join(sets, ", ")+fmt.Sprintf(" WHERE env_ref = $%d", len(columns)+1), args...)
		return err
	})
}

func (l *Ledger) directory(ctx context.Context, rec Record, state string) error {
	return l.Tx(ctx, PlatformID, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, "INSERT INTO sandbox_env_directory (env_ref, owner, backend, state, updated_at) VALUES ($1, $2, $3, $4, $5) "+
			"ON CONFLICT (env_ref) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at, backend = excluded.backend",
			rec.EnvRef, rec.Owner, rec.Backend, state, ISO(l.Now()))
		return err
	})
}
