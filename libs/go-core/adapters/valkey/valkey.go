// Package valkey 是 Cache 接口（设计书 4.5）的 Valkey 实现（CACHE-01，docs/plan/workspace-read-latency-and-disk-2026-11-03.md）。
// 语义见 providers/cache，与开发实现 dev/memcache 过同一套一致性测试。
//
// 键都在一个前缀（Options.Prefix，默认 ar:）下，按作用域分开：<前缀>{<作用域>}:<种类>:<名字>。花括号是 hash tag，
// 同一作用域的键落在同一个槽里，Lua 脚本可以同时动几个键。时间一律取 Valkey 自己的时钟（TIME、PX 过期），
// 几个 task-service 副本之间不用对表。
//
// 推流：每个流是一个 Valkey Stream，事件的 id 是“<序号>-0”，序号与已清理的最大序号记在旁边的 hash 里；
// 每个事件带自己的过期时刻，读与追加时先从头清理过期的事件，读到页内过期的也跳过并记作已清理。
// 订阅先补发、再用 XREAD BLOCK 等新事件，每个订阅单独一条连接（不占连接池）。
//
// 只存可丢失或可重建的数据（设计书 4.5）：部署时不要求持久化。
package valkey

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"strconv"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Options 是构造参数。
type Options struct {
	// URL 形如 valkey://:口令@主机:6379/0（TLS 用 valkeys://；也收 redis://、rediss://）。带口令，按密钥对待。
	URL secret.Value
	// Prefix 是所有键的前缀，几个环境共用一个 Valkey 时用来分开；默认 ar:。
	Prefix string
	// MaxConns 是连接池的上限（不含订阅各自的连接），默认 16。
	MaxConns int
	// DialTimeout 默认 5 秒；OpTimeout 是 ctx 没有期限时一条命令的上限，默认 10 秒。
	DialTimeout time.Duration
	OpTimeout   time.Duration
	// Clock 只用来算 Lock.ExpiresAt；过期本身由 Valkey 判定。为空时用真实时钟。
	Clock  clock.Clock
	Faults *testkit.Faults
}

// Provider 是 Valkey Cache 的工厂。打印时不带地址里的口令。
type Provider struct {
	prefix string
	pool   *pool
	clock  clock.Clock
	faults *testkit.Faults
	// subDial 给订阅单独建连接。
	subDial func(ctx context.Context) (*conn, error)
}

// New 解析地址并构造 Provider；不在这里连接（Ping 才连）。
func New(opts Options) (*Provider, error) {
	ep, err := parseURL(opts.URL)
	if err != nil {
		return nil, err
	}
	if opts.Prefix == "" {
		opts.Prefix = "ar:"
	}
	if opts.MaxConns <= 0 {
		opts.MaxConns = 16
	}
	if opts.DialTimeout <= 0 {
		opts.DialTimeout = 5 * time.Second
	}
	if opts.OpTimeout <= 0 {
		opts.OpTimeout = 10 * time.Second
	}
	if opts.Clock == nil {
		opts.Clock = clock.System{}
	}
	p := &Provider{prefix: opts.Prefix, pool: newPool(ep, opts.MaxConns, opts.DialTimeout, opts.OpTimeout), clock: opts.Clock, faults: opts.Faults}
	p.subDial = func(ctx context.Context) (*conn, error) { return dial(ctx, ep, opts.DialTimeout) }
	return p, nil
}

// String 不带地址与口令。
func (p *Provider) String() string { return "valkey.Provider{prefix:" + p.prefix + "}" }

// GoString 同 String（%#v 也不展开内部字段）。
func (p *Provider) GoString() string { return p.String() }

// Ping 连一次并 PING，启动时用来尽早发现配错的地址或口令。
func (p *Provider) Ping(ctx context.Context) error {
	reply, err := p.pool.do(ctx, "PING")
	if err != nil {
		return classify(err)
	}
	if s, _ := reply.(string); s != "PONG" {
		return providererr.New(providererr.Transient, "unavailable", "Valkey 没有回 PONG")
	}
	return nil
}

func (p *Provider) Bind(t *commonv1.TenantContext) (cache.Cache, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &client{p: p, base: p.prefix + "{" + devutil.ScopeID(s) + "}:"}, nil
}

func (p *Provider) BindPlatform(purpose string) (cache.Cache, error) {
	s, err := tenancy.PlatformScope(purpose)
	if err != nil {
		return nil, err
	}
	return &client{p: p, base: p.prefix + "{" + devutil.ScopeID(s) + "}:"}, nil
}

func (p *Provider) Close() error {
	p.pool.close()
	return nil
}

type client struct {
	p    *Provider
	base string
}

func (c *client) key(kind, name string) string { return c.base + kind + ":" + name }

func (c *client) enter(op string) error { return c.p.faults.Check(op) }

func (c *client) do(ctx context.Context, args ...any) (any, error) {
	reply, err := c.p.pool.do(ctx, args...)
	return reply, classify(err)
}

func (c *client) eval(ctx context.Context, s script, keys []string, args ...any) (any, error) {
	reply, err := c.p.pool.eval(ctx, s, keys, args...)
	return reply, classify(err)
}

// ms 把时长换成毫秒，不足 1 毫秒按 1 毫秒（PX 只收正整数）。
func ms(d time.Duration) int64 {
	n := int64((d + time.Millisecond - 1) / time.Millisecond)
	return max(n, 1)
}

func asInt(v any) int64 {
	switch x := v.(type) {
	case int64:
		return x
	case []byte:
		n, _ := strconv.ParseInt(string(x), 10, 64)
		return n
	case string:
		n, _ := strconv.ParseInt(x, 10, 64)
		return n
	}
	return 0
}

func randomToken(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// --- 键值 ---

func (c *client) Get(ctx context.Context, key string) ([]byte, bool, error) {
	if err := devutil.CheckName(key, "键"); err != nil {
		return nil, false, err
	}
	if err := c.enter("Get"); err != nil {
		return nil, false, err
	}
	reply, err := c.do(ctx, "GET", c.key("v", key))
	if err != nil || reply == nil {
		return nil, false, err
	}
	b, _ := reply.([]byte)
	return b, true, nil
}

func (c *client) Set(ctx context.Context, key string, value []byte, ttl time.Duration) error {
	if err := devutil.CheckName(key, "键"); err != nil {
		return err
	}
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return err
	}
	if err := c.enter("Set"); err != nil {
		return err
	}
	_, err := c.do(ctx, "SET", c.key("v", key), value, "PX", ms(ttl))
	return err
}

func (c *client) SetIfAbsent(ctx context.Context, key string, value []byte, ttl time.Duration) (bool, error) {
	if err := devutil.CheckName(key, "键"); err != nil {
		return false, err
	}
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return false, err
	}
	if err := c.enter("SetIfAbsent"); err != nil {
		return false, err
	}
	reply, err := c.do(ctx, "SET", c.key("v", key), value, "NX", "PX", ms(ttl))
	return err == nil && reply != nil, err
}

func (c *client) Delete(ctx context.Context, key string) (bool, error) {
	if err := devutil.CheckName(key, "键"); err != nil {
		return false, err
	}
	if err := c.enter("Delete"); err != nil {
		return false, err
	}
	reply, err := c.do(ctx, "DEL", c.key("v", key))
	return asInt(reply) > 0, err
}

// --- 计数与限流 ---

// incrScript：键不存在时从 0 开始并在创建时设过期；之后不改过期（PTTL 为 -1 只出现在刚创建时）。
var incrScript = newScript(`
local n = redis.call('INCRBY', KEYS[1], ARGV[1])
if redis.call('PTTL', KEYS[1]) == -1 then redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
return n`)

func (c *client) Incr(ctx context.Context, key string, by int64, ttl time.Duration) (int64, error) {
	if err := devutil.CheckName(key, "键"); err != nil {
		return 0, err
	}
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return 0, err
	}
	if err := c.enter("Incr"); err != nil {
		return 0, err
	}
	reply, err := c.eval(ctx, incrScript, []string{c.key("n", key)}, by, ms(ttl))
	return asInt(reply), err
}

// rateScript：滑动窗口。有序集合的分数是请求的时刻（微秒，Valkey 的时钟），成员是“cost:唯一串”；
// 先删掉窗口外的，再按 cost 求和；放行才记入，被拒的不计入。被拒时 RetryAfter 是最早一批腾出足够额度的时刻。
// 返回 {放行 1/0, remaining, retry_after 微秒}。微秒时刻有 16 位，交给 redis.call 前用 %.0f 转成字符串
// （Lua 5.1 默认按 %.14g 转，会丢精度）。
var rateScript = newScript(`
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000000 + tonumber(t[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', string.format('%.0f', now - window))
local hits = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
local used = 0
for i = 1, #hits, 2 do used = used + tonumber(string.match(hits[i], '^(%d+):')) end
if used + cost <= limit then
  redis.call('ZADD', KEYS[1], string.format('%.0f', now), cost .. ':' .. ARGV[4])
  redis.call('PEXPIRE', KEYS[1], math.ceil(window / 1000) + 1)
  return {1, limit - used - cost, 0}
end
local retry = window
local freed = 0
for i = 1, #hits, 2 do
  freed = freed + tonumber(string.match(hits[i], '^(%d+):'))
  if used - freed + cost <= limit then
    retry = tonumber(hits[i + 1]) + window - now
    break
  end
end
if retry <= 0 then retry = 1 end
local remaining = limit - used
if remaining < 0 then remaining = 0 end
return {0, remaining, retry}`)

func (c *client) RateLimit(ctx context.Context, key string, limit int64, window time.Duration, cost int64) (cache.RateDecision, error) {
	if err := devutil.CheckName(key, "键"); err != nil {
		return cache.RateDecision{}, err
	}
	if err := devutil.CheckPositive(window, "window"); err != nil {
		return cache.RateDecision{}, err
	}
	if limit < 1 || cost < 1 {
		return cache.RateDecision{}, providererr.New(providererr.Invalid, "invalid_argument", "limit 与 cost 必须至少为 1")
	}
	if err := c.enter("RateLimit"); err != nil {
		return cache.RateDecision{}, err
	}
	micros := max(window.Microseconds(), 1)
	reply, err := c.eval(ctx, rateScript, []string{c.key("r", key)}, limit, micros, cost, randomToken(8))
	if err != nil {
		return cache.RateDecision{}, err
	}
	parts, _ := reply.([]any)
	if len(parts) != 3 {
		return cache.RateDecision{}, providererr.New(providererr.Transient, "unavailable", "Valkey 的限流脚本返回格式不对")
	}
	d := cache.RateDecision{Allowed: asInt(parts[0]) == 1, Remaining: asInt(parts[1])}
	if !d.Allowed {
		d.RetryAfter = time.Duration(asInt(parts[2])) * time.Microsecond
	}
	return d, nil
}

// --- 锁 ---

var extendScript = newScript(`
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0`)

var releaseScript = newScript(`
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0`)

func (c *client) AcquireLock(ctx context.Context, name string, ttl time.Duration) (*cache.Lock, error) {
	if err := devutil.CheckName(name, "锁名"); err != nil {
		return nil, err
	}
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return nil, err
	}
	if err := c.enter("AcquireLock"); err != nil {
		return nil, err
	}
	t := randomToken(16)
	reply, err := c.do(ctx, "SET", c.key("l", name), t, "NX", "PX", ms(ttl))
	if err != nil || reply == nil {
		return nil, err
	}
	return &cache.Lock{Name: name, Token: t, ExpiresAt: c.p.clock.Now().Add(ttl)}, nil
}

func (c *client) ExtendLock(ctx context.Context, lock cache.Lock, ttl time.Duration) (*cache.Lock, error) {
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return nil, err
	}
	if err := c.enter("ExtendLock"); err != nil {
		return nil, err
	}
	if devutil.CheckName(lock.Name, "锁名") != nil {
		return nil, nil
	}
	reply, err := c.eval(ctx, extendScript, []string{c.key("l", lock.Name)}, lock.Token, ms(ttl))
	if err != nil || asInt(reply) != 1 {
		return nil, err
	}
	return &cache.Lock{Name: lock.Name, Token: lock.Token, ExpiresAt: c.p.clock.Now().Add(ttl)}, nil
}

func (c *client) ReleaseLock(ctx context.Context, lock cache.Lock) (bool, error) {
	if err := c.enter("ReleaseLock"); err != nil {
		return false, err
	}
	if devutil.CheckName(lock.Name, "锁名") != nil {
		return false, nil
	}
	reply, err := c.eval(ctx, releaseScript, []string{c.key("l", lock.Name)}, lock.Token)
	return err == nil && asInt(reply) == 1, err
}

// --- 推流 ---

// streamLib 是追加与读取共用的 Lua：取 Valkey 的时刻（毫秒），从头清理过期事件并更新已清理的最大序号。
// KEYS[1] 是流，KEYS[2] 是旁边的 hash（s 是序号，t 是已清理的最大序号）。事件的字段固定是 d（数据）、x（过期时刻）。
const streamLib = `
local function now_ms()
  local t = redis.call('TIME')
  return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end
local function seq_of(id) return tonumber(string.match(id, '^(%d+)-')) end
local function trim_head(now)
  local trimmed = tonumber(redis.call('HGET', KEYS[2], 't') or '0')
  local changed = false
  while true do
    local head = redis.call('XRANGE', KEYS[1], '-', '+', 'COUNT', 64)
    local stop = #head == 0
    for _, e in ipairs(head) do
      if tonumber(e[2][4]) > now then stop = true break end
      redis.call('XDEL', KEYS[1], e[1])
      local s = seq_of(e[1])
      if s > trimmed then trimmed = s end
      changed = true
    end
    if stop then break end
  end
  -- 事件也可能随流的键整体过期而消失：比第一个还在的事件小的序号（流空了就是最后分配的序号）都算已清理。
  local first = redis.call('XRANGE', KEYS[1], '-', '+', 'COUNT', 1)
  local floor
  if #first == 0 then floor = tonumber(redis.call('HGET', KEYS[2], 's') or '0') else floor = seq_of(first[1][1]) - 1 end
  if floor > trimmed then trimmed = floor changed = true end
  return trimmed, changed
end
`

// appendScript：ARGV[1] 数据，ARGV[2] 保留毫秒，ARGV[3] 旁边 hash 的保留毫秒。返回新事件的序号。
// 流本身的过期跟着最长寿的事件走；序号与已清理的最大序号保留得更久，过了保留期再追加的事件序号也不回退。
var appendScript = newScript(streamLib + `
local now = now_ms()
local trimmed, changed = trim_head(now)
if changed then redis.call('HSET', KEYS[2], 't', trimmed) end
local seq = redis.call('HINCRBY', KEYS[2], 's', 1)
local keep = tonumber(ARGV[2])
redis.call('XADD', KEYS[1], seq .. '-0', 'd', ARGV[1], 'x', now + keep)
if redis.call('PTTL', KEYS[1]) < keep then redis.call('PEXPIRE', KEYS[1], keep) end
local book = tonumber(ARGV[3])
if redis.call('PTTL', KEYS[2]) < book then redis.call('PEXPIRE', KEYS[2], book) end
return seq`)

// readScript：ARGV[1] after，ARGV[2] limit。返回 {gap 1/0, 序号, 数据, 序号, 数据, …}。
var readScript = newScript(streamLib + `
local now = now_ms()
local trimmed, changed = trim_head(now)
local limit = tonumber(ARGV[2])
local out = {}
local start = '(' .. ARGV[1] .. '-0'
while #out < limit * 2 do
  local page = redis.call('XRANGE', KEYS[1], start, '+', 'COUNT', limit)
  for _, e in ipairs(page) do
    local s = seq_of(e[1])
    if tonumber(e[2][4]) <= now then
      redis.call('XDEL', KEYS[1], e[1])
      if s > trimmed then trimmed = s end
      changed = true
    elseif #out < limit * 2 then
      table.insert(out, s)
      table.insert(out, e[2][2])
    end
    start = '(' .. e[1]
  end
  if #page < limit then break end
end
if changed then redis.call('HSET', KEYS[2], 't', trimmed) end
local gap = 0
if trimmed > tonumber(ARGV[1]) then gap = 1 end
table.insert(out, 1, gap)
return out`)

func (c *client) streamKeys(name string) []string {
	return []string{c.key("e", name), c.key("em", name)}
}

func (c *client) AppendEvent(ctx context.Context, name string, data []byte, retention time.Duration) (int64, error) {
	if err := devutil.CheckName(name, "流名"); err != nil {
		return 0, err
	}
	if err := devutil.CheckPositive(retention, "retention"); err != nil {
		return 0, err
	}
	if err := c.enter("AppendEvent"); err != nil {
		return 0, err
	}
	keep := ms(retention)
	reply, err := c.eval(ctx, appendScript, c.streamKeys(name), data, keep, 2*keep+ms(time.Hour))
	return asInt(reply), err
}

func (c *client) readEvents(ctx context.Context, name string, after int64, limit int) (cache.EventPage, error) {
	reply, err := c.eval(ctx, readScript, c.streamKeys(name), after, limit)
	if err != nil {
		return cache.EventPage{}, err
	}
	parts, _ := reply.([]any)
	if len(parts) == 0 || len(parts)%2 != 1 {
		return cache.EventPage{}, providererr.New(providererr.Transient, "unavailable", "Valkey 的读流脚本返回格式不对")
	}
	page := cache.EventPage{Gap: asInt(parts[0]) == 1}
	for i := 1; i+1 < len(parts); i += 2 {
		data, _ := parts[i+1].([]byte)
		page.Events = append(page.Events, cache.StreamEvent{ID: asInt(parts[i]), Data: data})
	}
	return page, nil
}

func (c *client) ReadEvents(ctx context.Context, name string, after int64, limit int) (cache.EventPage, error) {
	if err := devutil.CheckName(name, "流名"); err != nil {
		return cache.EventPage{}, err
	}
	if after < 0 || limit < 1 {
		return cache.EventPage{}, providererr.New(providererr.Invalid, "invalid_argument", "after 不能为负，limit 至少为 1")
	}
	if err := c.enter("ReadEvents"); err != nil {
		return cache.EventPage{}, err
	}
	return c.readEvents(ctx, name, after, limit)
}

func (c *client) Subscribe(_ context.Context, name string, after int64) (cache.Subscription, error) {
	if err := devutil.CheckName(name, "流名"); err != nil {
		return nil, err
	}
	if after < 0 {
		return nil, providererr.New(providererr.Invalid, "invalid_argument", "after 不能为负")
	}
	if err := c.enter("Subscribe"); err != nil {
		return nil, err
	}
	return &subscription{c: c, name: name, last: after, closed: make(chan struct{})}, nil
}

// blockStep 是一次 XREAD BLOCK 的最长等待；等满了回头检查关闭与 ctx 再等。
const blockStep = time.Second

type subscription struct {
	c    *client
	name string
	last int64

	mu     sync.Mutex
	conn   *conn
	once   sync.Once
	closed chan struct{}
}

func (s *subscription) isClosed() bool {
	select {
	case <-s.closed:
		return true
	default:
		return false
	}
}

func errClosed() error { return providererr.New(providererr.Invalid, "closed", "订阅已关闭") }

func (s *subscription) Next(ctx context.Context) (cache.StreamEvent, error) {
	for {
		if s.isClosed() {
			return cache.StreamEvent{}, errClosed()
		}
		if err := ctx.Err(); err != nil {
			return cache.StreamEvent{}, classify(err)
		}
		page, err := s.c.readEvents(ctx, s.name, s.last, 1)
		if err != nil {
			if s.isClosed() {
				return cache.StreamEvent{}, errClosed()
			}
			return cache.StreamEvent{}, err
		}
		if len(page.Events) > 0 {
			s.last = page.Events[0].ID
			return page.Events[0], nil
		}
		if err := s.wait(ctx); err != nil {
			if s.isClosed() {
				return cache.StreamEvent{}, errClosed()
			}
			return cache.StreamEvent{}, err
		}
	}
}

// wait 在自己的连接上 XREAD BLOCK，直到流里出现序号大于 last 的事件、等满 blockStep、ctx 到期或订阅关闭。
// 读到的内容不直接用：回到 Next 经读流脚本取，过期判定与 ReadEvents 一致。
func (s *subscription) wait(ctx context.Context) error {
	s.mu.Lock()
	if s.conn == nil {
		c, err := s.c.p.subDial(ctx)
		if err != nil {
			s.mu.Unlock()
			return classify(err)
		}
		s.conn = c
	}
	c := s.conn
	s.mu.Unlock()

	block := blockStep
	if d, ok := ctx.Deadline(); ok {
		block = min(block, time.Until(d))
	}
	block = max(block, 10*time.Millisecond)
	_ = c.nc.SetDeadline(time.Now().Add(block + 5*time.Second))
	reply, err := c.roundTrip([]any{"XREAD", "COUNT", 1, "BLOCK", block.Milliseconds(), "STREAMS", s.c.key("e", s.name), strconv.FormatInt(s.last, 10) + "-0"})
	if err == nil {
		if se, ok := reply.(serverError); ok {
			err = se
		}
	}
	if err != nil {
		s.dropConn(c)
		if ctx.Err() != nil {
			return classify(ctx.Err())
		}
		return classify(err)
	}
	return nil
}

func (s *subscription) dropConn(c *conn) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn == c {
		s.conn = nil
	}
	c.close()
}

// Close 关掉订阅；正阻塞在 XREAD 上的 Next 随连接关闭立即返回。
func (s *subscription) Close() error {
	s.once.Do(func() {
		close(s.closed)
		s.mu.Lock()
		if s.conn != nil {
			s.conn.close()
			s.conn = nil
		}
		s.mu.Unlock()
	})
	return nil
}
