// Package grants 是出网网关读写业务库的部分（设计书改动稿 W8、W9；docs/plan/sandbox-platform.md 第 2 节）：
//
//   - 环境凭据只说明是哪个租户的哪个环境；环境表（sandbox_envs）给出它现在是否还在、最近由哪个 Task 使用，
//     联网授权（sandbox_grants）按 Task 查。授权属于 Task：环境被下一个 Task 用了，就按那个 Task 的授权放行。
//   - 查到的结果缓存 TTL（默认 2 秒）：撤销、到期、Task 结束在几秒内生效；管理接口收回时立即作废缓存（Invalidate）。
//   - 每个授权一个计量（Meter）：已用的上行、下行字节（从表里的 up_bytes、down_bytes 接着算）、正在开着的连接数、每秒请求的令牌桶。
//     Flush 定期把增量写回 sandbox_grants 与当天的 sandbox_usage.egress_bytes（W10 的每天出网配额读它）。
//   - 审计（Audit）：每个连接一行 sandbox_egress_audit。
//
// 表都由 Worker 迁移（agentruntime_worker.sandbox.ledger 的 201～203）；这里不迁移，表不在时报 transient。
package grants

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// ISO 是表里的时间写法（与 Worker 的 ledger.iso 相同，按字典序可比）。
func ISO(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05.000000Z") }

func parseISO(s string) time.Time {
	t, err := time.Parse("2006-01-02T15:04:05.000000Z", s)
	if err != nil {
		return time.Time{}
	}
	return t
}

// Grant 是一条联网授权。
type Grant struct {
	ID            string
	TaskID        string
	Hosts         []string
	Ports         []int
	ExpiresAt     time.Time
	MaxUpBytes    int64
	MaxDownBytes  int64
	MaxConns      int64
	MaxRPS        int64
	UpBytes       int64
	DownBytes     int64
	PreviewPort   int
	PreviewEnv    string
	PreviewTarget string
}

// Active：授权还在期限内。
func (g Grant) Active(now time.Time) bool { return now.Before(g.ExpiresAt) }

// Session 是一个环境此刻的样子：正在用它的 Task 与那个 Task 的有效授权（按 grant_id 排好序）。
type Session struct {
	Tenant string
	EnvRef string
	TaskID string
	Grants []Grant
}

type cacheKey struct{ tenant, env string }

type cached struct {
	at   time.Time
	sess Session
	err  error
}

// Store 是带缓存的读写。
type Store struct {
	db  relational.Provider
	ttl time.Duration
	now func() time.Time

	mu     sync.Mutex
	cache  map[cacheKey]cached
	meters map[string]*Meter
	usage  map[usageKey]*dayUsage
}

// New 建一个 Store；ttl 是授权缓存的时长。
func New(db relational.Provider, ttl time.Duration, now func() time.Time) *Store {
	if now == nil {
		now = time.Now
	}
	return &Store{db: db, ttl: ttl, now: now, cache: map[cacheKey]cached{}, meters: map[string]*Meter{}, usage: map[usageKey]*dayUsage{}}
}

func (s *Store) bind(tenant string) (relational.Store, error) {
	if tenant == "_platform" {
		return s.db.BindPlatform("egress-gateway")
	}
	return s.db.Bind(&commonv1.TenantContext{TenantId: tenant})
}

// missingTable：SQLite 报 no such table，Postgres 报 relation does not exist（适配器映射成 invalid，原因在包着的错误里）。
func missingTable(err error) bool {
	for e := err; e != nil; e = errors.Unwrap(e) {
		text := strings.ToLower(e.Error())
		if strings.Contains(text, "no such table") || (strings.Contains(text, "relation") && strings.Contains(text, "does not exist")) {
			return true
		}
	}
	return false
}

func storeError(err error) error {
	if err != nil && missingTable(err) {
		return providererr.Wrap(providererr.Transient, "sandbox_tables_missing", "沙箱的表还没建好（Worker 起来后会迁移）", err)
	}
	return err
}

// ErrEnvGone：环境不在了（销毁、丢失或从没有过）；凭据还没过期也拒绝。
var ErrEnvGone = providererr.New(providererr.Denied, "env_not_active", "环境已经不在了")

// Lookup 给出环境此刻的 Task 与授权（缓存 ttl）。
func (s *Store) Lookup(ctx context.Context, tenant, envRef string) (Session, error) {
	key := cacheKey{tenant, envRef}
	// 缓存按真实流逝的时间（单调时钟）老化；授权的到期按注入的时钟（测试里会拨快）。
	s.mu.Lock()
	if c, ok := s.cache[key]; ok && time.Since(c.at) < s.ttl {
		s.mu.Unlock()
		return c.sess, c.err
	}
	s.mu.Unlock()
	sess, err := s.load(ctx, tenant, envRef, s.now())
	if err != nil && providererr.CategoryOf(err) == providererr.Transient {
		return sess, err // 不缓存暂时性的错误
	}
	s.mu.Lock()
	s.cache[key] = cached{at: time.Now(), sess: sess, err: err}
	s.mu.Unlock()
	return sess, err
}

// Invalidate 作废一个租户的缓存（收回授权之后）。
func (s *Store) Invalidate(tenant string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for k := range s.cache {
		if k.tenant == tenant {
			delete(s.cache, k)
		}
	}
}

const grantColumns = `grant_id, task_id, hosts, ports, expires_at, max_up_bytes, max_down_bytes, max_conns, max_rps, up_bytes, down_bytes,
	preview_port, preview_env, preview_target`

func (s *Store) load(ctx context.Context, tenant, envRef string, now time.Time) (Session, error) {
	db, err := s.bind(tenant)
	if err != nil {
		return Session{}, err
	}
	sess := Session{Tenant: tenant, EnvRef: envRef}
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, "SELECT task_id, state FROM sandbox_envs WHERE env_ref = $1", envRef)
		if err != nil {
			return err
		}
		if !ok || str(row["state"]) != "ready" {
			return ErrEnvGone
		}
		sess.TaskID = str(row["task_id"])
		if sess.TaskID == "" {
			return nil
		}
		rows, err := tx.Query(ctx, "SELECT "+grantColumns+" FROM sandbox_grants WHERE task_id = $1 AND state = 'active' AND expires_at > $2 ORDER BY grant_id",
			sess.TaskID, ISO(now))
		if err != nil {
			return err
		}
		for _, r := range rows {
			sess.Grants = append(sess.Grants, grantFrom(r))
		}
		return nil
	})
	return sess, storeError(err)
}

// PreviewGrant 找这个环境这个端口的预览授权（有效的那一个）。
func (s *Store) PreviewGrant(ctx context.Context, tenant, envRef string, port int) (Grant, bool, error) {
	db, err := s.bind(tenant)
	if err != nil {
		return Grant{}, false, err
	}
	var out Grant
	found := false
	now := s.now()
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		env, ok, err := tx.QueryOne(ctx, "SELECT state FROM sandbox_envs WHERE env_ref = $1", envRef)
		if err != nil {
			return err
		}
		if !ok || str(env["state"]) != "ready" {
			return nil
		}
		rows, err := tx.Query(ctx, "SELECT "+grantColumns+" FROM sandbox_grants WHERE preview_env = $1 AND preview_port = $2 AND state = 'active' AND expires_at > $3 ORDER BY grant_id",
			envRef, int64(port), ISO(now))
		if err != nil || len(rows) == 0 {
			return err
		}
		out, found = grantFrom(rows[0]), true
		return nil
	})
	return out, found, storeError(err)
}

// Revoke 收回一个 Task 的全部授权（管理接口），返回收回了几个，并作废这个租户的缓存。
func (s *Store) Revoke(ctx context.Context, tenant, taskID string) (int64, error) {
	db, err := s.bind(tenant)
	if err != nil {
		return 0, err
	}
	var n int64
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		var err error
		n, err = tx.Execute(ctx, "UPDATE sandbox_grants SET state = 'revoked', revoked_at = $1 WHERE task_id = $2 AND state = 'active'", ISO(s.now()), taskID)
		return err
	})
	s.Invalidate(tenant)
	return n, storeError(err)
}

func grantFrom(r relational.Row) Grant {
	g := Grant{
		ID: str(r["grant_id"]), TaskID: str(r["task_id"]), ExpiresAt: parseISO(str(r["expires_at"])),
		MaxUpBytes: num(r["max_up_bytes"]), MaxDownBytes: num(r["max_down_bytes"]), MaxConns: num(r["max_conns"]), MaxRPS: num(r["max_rps"]),
		UpBytes: num(r["up_bytes"]), DownBytes: num(r["down_bytes"]), PreviewPort: int(num(r["preview_port"])),
		PreviewEnv: str(r["preview_env"]), PreviewTarget: str(r["preview_target"]),
	}
	_ = json.Unmarshal([]byte(str(r["hosts"])), &g.Hosts)
	_ = json.Unmarshal([]byte(str(r["ports"])), &g.Ports)
	sort.Strings(g.Hosts)
	return g
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func num(v any) int64 {
	switch n := v.(type) {
	case int64:
		return n
	case float64:
		return int64(n)
	}
	return 0
}

// --- 计量 ---

// Meter 是一个授权的用量：从表里已有的数接着算。并发安全。
type Meter struct {
	mu        sync.Mutex
	grant     string
	tenant    string
	up, down  int64 // 累计（含表里已有的）
	flushedUp int64 // 已写回的
	flushedDn int64
	active    int64
	tokens    float64
	last      time.Time
}

// Meter 取（或建）一个授权的计量。
func (s *Store) Meter(tenant string, g Grant) *Meter {
	s.mu.Lock()
	defer s.mu.Unlock()
	m := s.meters[g.ID]
	if m == nil {
		m = &Meter{grant: g.ID, tenant: tenant, up: g.UpBytes, down: g.DownBytes, flushedUp: g.UpBytes, flushedDn: g.DownBytes}
		s.meters[g.ID] = m
	}
	return m
}

// Open 开一个连接：并发数与每秒请求数都没超时登记并返回空，否则给出原因。
func (m *Meter) Open(g Grant, now time.Time) string {
	m.mu.Lock()
	defer m.mu.Unlock()
	if g.MaxUpBytes > 0 && m.up >= g.MaxUpBytes {
		return "up_bytes_exceeded"
	}
	if g.MaxDownBytes > 0 && m.down >= g.MaxDownBytes {
		return "down_bytes_exceeded"
	}
	if g.MaxConns > 0 && m.active >= g.MaxConns {
		return "too_many_connections"
	}
	if g.MaxRPS > 0 {
		if m.last.IsZero() {
			m.tokens = float64(g.MaxRPS)
		} else {
			m.tokens = min(float64(g.MaxRPS), m.tokens+now.Sub(m.last).Seconds()*float64(g.MaxRPS))
		}
		m.last = now
		if m.tokens < 1 {
			return "rate_limited"
		}
		m.tokens--
	}
	m.active++
	return ""
}

// Close 关一个连接。
func (m *Meter) Close() {
	m.mu.Lock()
	m.active--
	m.mu.Unlock()
}

// Add 记上行、下行的字节；超过授权的上限时给出原因（调用方断开连接）。
func (m *Meter) Add(g Grant, up, down int64) string {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.up += up
	m.down += down
	if g.MaxUpBytes > 0 && m.up > g.MaxUpBytes {
		return "up_bytes_exceeded"
	}
	if g.MaxDownBytes > 0 && m.down > g.MaxDownBytes {
		return "down_bytes_exceeded"
	}
	return ""
}

// Totals 是已用的上行、下行字节。
func (m *Meter) Totals() (int64, int64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.up, m.down
}

// --- 每天的出网字节（W10） ---

type usageKey struct{ tenant, day string }

type dayUsage struct {
	base, pending int64
	loaded        bool
}

func day(t time.Time) string { return t.UTC().Format("2006-01-02") }

// UsedToday 是这个租户今天经网关的字节数（表里的加上还没写回的）。
func (s *Store) UsedToday(ctx context.Context, tenant string) int64 {
	k := usageKey{tenant, day(s.now())}
	s.mu.Lock()
	u := s.usage[k]
	if u == nil {
		u = &dayUsage{}
		s.usage[k] = u
	}
	loaded := u.loaded
	s.mu.Unlock()
	if !loaded && tenant != "_platform" {
		if db, err := s.bind(tenant); err == nil {
			_ = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
				row, ok, err := tx.QueryOne(ctx, "SELECT egress_bytes FROM sandbox_usage WHERE day = $1", k.day)
				if err == nil && ok {
					s.mu.Lock()
					if !u.loaded {
						u.base, u.loaded = num(row["egress_bytes"]), true
					}
					s.mu.Unlock()
				} else if err == nil {
					s.mu.Lock()
					u.loaded = true
					s.mu.Unlock()
				}
				return nil
			})
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return u.base + u.pending
}

// AddUsage 记一个租户今天经网关的字节数。
func (s *Store) AddUsage(tenant string, n int64) {
	if n <= 0 || tenant == "_platform" {
		return
	}
	k := usageKey{tenant, day(s.now())}
	s.mu.Lock()
	defer s.mu.Unlock()
	u := s.usage[k]
	if u == nil {
		u = &dayUsage{}
		s.usage[k] = u
	}
	u.pending += n
}

// Flush 把授权的用量增量与每天的出网字节写回业务库；返回写不进的个数（下次再写）。
func (s *Store) Flush(ctx context.Context) int {
	type grantDelta struct {
		m        *Meter
		up, down int64
	}
	var grants []grantDelta
	s.mu.Lock()
	for _, m := range s.meters {
		m.mu.Lock()
		if du, dd := m.up-m.flushedUp, m.down-m.flushedDn; du > 0 || dd > 0 {
			grants = append(grants, grantDelta{m, du, dd})
		}
		m.mu.Unlock()
	}
	type usageDelta struct {
		k usageKey
		n int64
	}
	var usages []usageDelta
	for k, u := range s.usage {
		if u.pending > 0 {
			usages = append(usages, usageDelta{k, u.pending})
		}
	}
	s.mu.Unlock()
	failed := 0
	for _, g := range grants {
		db, err := s.bind(g.m.tenant)
		if err == nil {
			err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
				_, err := tx.Execute(ctx, "UPDATE sandbox_grants SET up_bytes = up_bytes + $1, down_bytes = down_bytes + $2 WHERE grant_id = $3",
					g.up, g.down, g.m.grant)
				return err
			})
		}
		if err != nil {
			failed++
			continue
		}
		g.m.mu.Lock()
		g.m.flushedUp += g.up
		g.m.flushedDn += g.down
		g.m.mu.Unlock()
	}
	for _, u := range usages {
		db, err := s.bind(u.k.tenant)
		if err == nil {
			err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
				_, err := tx.Execute(ctx, "INSERT INTO sandbox_usage (day, egress_bytes) VALUES ($1, $2) "+
					"ON CONFLICT (day) DO UPDATE SET egress_bytes = sandbox_usage.egress_bytes + excluded.egress_bytes", u.k.day, u.n)
				return err
			})
		}
		if err != nil {
			failed++
			continue
		}
		s.mu.Lock()
		if cur := s.usage[u.k]; cur != nil {
			cur.pending -= u.n
			cur.base += u.n
		}
		s.mu.Unlock()
	}
	today := day(s.now())
	s.mu.Lock()
	for k, u := range s.usage {
		if k.day != today && u.pending == 0 {
			delete(s.usage, k)
		}
	}
	s.mu.Unlock()
	return failed
}

// --- 审计 ---

// Record 是一个连接的审计记录（不含凭据、请求与响应的内容）。
type Record struct {
	ID         string
	Tenant     string
	GrantID    string
	TaskID     string
	EnvRef     string
	Direction  string // egress 或 preview
	Host       string
	Port       int
	Address    string
	Started    time.Time
	Duration   time.Duration
	UpBytes    int64
	DownBytes  int64
	Result     string // allowed、denied、closed
	Reason     string
}

// Audit 写一行 sandbox_egress_audit。
func (s *Store) Audit(ctx context.Context, r Record) error {
	if r.Tenant == "" || r.EnvRef == "" {
		return nil
	}
	db, err := s.bind(r.Tenant)
	if err != nil {
		return err
	}
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `INSERT INTO sandbox_egress_audit (audit_id, grant_id, task_id, env_ref, direction, host, port, address,
			started_at, duration_ms, up_bytes, down_bytes, result, reason) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)`,
			r.ID, r.GrantID, r.TaskID, r.EnvRef, r.Direction, r.Host, int64(r.Port), r.Address, ISO(r.Started), r.Duration.Milliseconds(),
			r.UpBytes, r.DownBytes, r.Result, r.Reason)
		return err
	})
	return storeError(err)
}
