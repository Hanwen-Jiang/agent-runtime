// Package memcache 是内存 Cache（开发实现）。语义见 providers/cache。
package memcache

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Options 是构造参数；Clock 为空时用真实时钟。
type Options struct {
	Clock  clock.Clock
	Faults *testkit.Faults
}

type entry struct {
	value   []byte
	expires time.Time
}

type counter struct {
	value   int64
	expires time.Time
}

type hit struct {
	at   time.Time
	cost int64
}

type held struct {
	token   string
	expires time.Time
}

type event struct {
	id      int64
	data    []byte
	expires time.Time
}

type stream struct {
	nextID     int64
	events     []event
	trimmedMax int64
	changed    chan struct{}
}

type scopeState struct {
	values   map[string]entry
	counters map[string]counter
	windows  map[string][]hit
	locks    map[string]held
	streams  map[string]*stream
}

// Provider 是内存 Cache 的工厂。
type Provider struct {
	mu     sync.Mutex
	clock  clock.Clock
	faults *testkit.Faults
	scopes map[string]*scopeState
}

// New 构造一个内存 Cache。
func New(opts Options) *Provider {
	c := opts.Clock
	if c == nil {
		c = clock.System{}
	}
	return &Provider{clock: c, faults: opts.Faults, scopes: map[string]*scopeState{}}
}

func (p *Provider) Bind(t *commonv1.TenantContext) (cache.Cache, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &client{p: p, owner: devutil.ScopeID(s)}, nil
}

func (p *Provider) BindPlatform(purpose string) (cache.Cache, error) {
	s, err := tenancy.PlatformScope(purpose)
	if err != nil {
		return nil, err
	}
	return &client{p: p, owner: devutil.ScopeID(s)}, nil
}

func (p *Provider) Close() error { return nil }

func (p *Provider) state(owner string) *scopeState {
	s, ok := p.scopes[owner]
	if !ok {
		s = &scopeState{
			values: map[string]entry{}, counters: map[string]counter{}, windows: map[string][]hit{},
			locks: map[string]held{}, streams: map[string]*stream{},
		}
		p.scopes[owner] = s
	}
	return s
}

type client struct {
	p     *Provider
	owner string
}

// enter 检查故障并加锁；调用方负责 defer p.mu.Unlock()。
func (c *client) enter(op string) (*scopeState, time.Time, error) {
	if err := c.p.faults.Check(op); err != nil {
		return nil, time.Time{}, err
	}
	c.p.mu.Lock()
	return c.p.state(c.owner), c.p.clock.Now(), nil
}

func token() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func (c *client) Get(_ context.Context, key string) ([]byte, bool, error) {
	if err := devutil.CheckName(key, "键"); err != nil {
		return nil, false, err
	}
	s, now, err := c.enter("Get")
	if err != nil {
		return nil, false, err
	}
	defer c.p.mu.Unlock()
	e, ok := s.values[key]
	if !ok || !e.expires.After(now) {
		delete(s.values, key)
		return nil, false, nil
	}
	return append([]byte(nil), e.value...), true, nil
}

func (c *client) Set(_ context.Context, key string, value []byte, ttl time.Duration) error {
	if err := devutil.CheckName(key, "键"); err != nil {
		return err
	}
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return err
	}
	s, now, err := c.enter("Set")
	if err != nil {
		return err
	}
	defer c.p.mu.Unlock()
	s.values[key] = entry{value: append([]byte(nil), value...), expires: now.Add(ttl)}
	return nil
}

func (c *client) SetIfAbsent(_ context.Context, key string, value []byte, ttl time.Duration) (bool, error) {
	if err := devutil.CheckName(key, "键"); err != nil {
		return false, err
	}
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return false, err
	}
	s, now, err := c.enter("SetIfAbsent")
	if err != nil {
		return false, err
	}
	defer c.p.mu.Unlock()
	if e, ok := s.values[key]; ok && e.expires.After(now) {
		return false, nil
	}
	s.values[key] = entry{value: append([]byte(nil), value...), expires: now.Add(ttl)}
	return true, nil
}

func (c *client) Delete(_ context.Context, key string) (bool, error) {
	if err := devutil.CheckName(key, "键"); err != nil {
		return false, err
	}
	s, now, err := c.enter("Delete")
	if err != nil {
		return false, err
	}
	defer c.p.mu.Unlock()
	e, ok := s.values[key]
	delete(s.values, key)
	return ok && e.expires.After(now), nil
}

func (c *client) Incr(_ context.Context, key string, by int64, ttl time.Duration) (int64, error) {
	if err := devutil.CheckName(key, "键"); err != nil {
		return 0, err
	}
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return 0, err
	}
	s, now, err := c.enter("Incr")
	if err != nil {
		return 0, err
	}
	defer c.p.mu.Unlock()
	cur, ok := s.counters[key]
	if !ok || !cur.expires.After(now) {
		cur = counter{expires: now.Add(ttl)}
	}
	cur.value += by
	s.counters[key] = cur
	return cur.value, nil
}

func (c *client) RateLimit(_ context.Context, key string, limit int64, window time.Duration, cost int64) (cache.RateDecision, error) {
	if err := devutil.CheckName(key, "键"); err != nil {
		return cache.RateDecision{}, err
	}
	if err := devutil.CheckPositive(window, "window"); err != nil {
		return cache.RateDecision{}, err
	}
	if limit < 1 || cost < 1 {
		return cache.RateDecision{}, providererr.New(providererr.Invalid, "invalid_argument", "limit 与 cost 必须至少为 1")
	}
	s, now, err := c.enter("RateLimit")
	if err != nil {
		return cache.RateDecision{}, err
	}
	defer c.p.mu.Unlock()
	cutoff := now.Add(-window)
	var hits []hit
	var used int64
	for _, h := range s.windows[key] {
		if h.at.After(cutoff) {
			hits = append(hits, h)
			used += h.cost
		}
	}
	if used+cost <= limit {
		s.windows[key] = append(hits, hit{at: now, cost: cost})
		return cache.RateDecision{Allowed: true, Remaining: limit - used - cost}, nil
	}
	s.windows[key] = hits
	retry := window
	var freed int64
	for _, h := range hits {
		freed += h.cost
		if used-freed+cost <= limit {
			retry = h.at.Add(window).Sub(now)
			break
		}
	}
	if retry <= 0 {
		retry = time.Microsecond
	}
	return cache.RateDecision{Allowed: false, Remaining: max(limit-used, 0), RetryAfter: retry}, nil
}

func (c *client) AcquireLock(_ context.Context, name string, ttl time.Duration) (*cache.Lock, error) {
	if err := devutil.CheckName(name, "锁名"); err != nil {
		return nil, err
	}
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return nil, err
	}
	s, now, err := c.enter("AcquireLock")
	if err != nil {
		return nil, err
	}
	defer c.p.mu.Unlock()
	if h, ok := s.locks[name]; ok && h.expires.After(now) {
		return nil, nil
	}
	t := token()
	s.locks[name] = held{token: t, expires: now.Add(ttl)}
	return &cache.Lock{Name: name, Token: t, ExpiresAt: now.Add(ttl)}, nil
}

func (c *client) ExtendLock(_ context.Context, lock cache.Lock, ttl time.Duration) (*cache.Lock, error) {
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return nil, err
	}
	s, now, err := c.enter("ExtendLock")
	if err != nil {
		return nil, err
	}
	defer c.p.mu.Unlock()
	h, ok := s.locks[lock.Name]
	if !ok || h.token != lock.Token || !h.expires.After(now) {
		return nil, nil
	}
	s.locks[lock.Name] = held{token: lock.Token, expires: now.Add(ttl)}
	return &cache.Lock{Name: lock.Name, Token: lock.Token, ExpiresAt: now.Add(ttl)}, nil
}

func (c *client) ReleaseLock(_ context.Context, lock cache.Lock) (bool, error) {
	s, now, err := c.enter("ReleaseLock")
	if err != nil {
		return false, err
	}
	defer c.p.mu.Unlock()
	h, ok := s.locks[lock.Name]
	if !ok || h.token != lock.Token || !h.expires.After(now) {
		return false, nil
	}
	delete(s.locks, lock.Name)
	return true, nil
}

// streamLocked 取出流并清理过期事件；调用方持有锁。
func (s *scopeState) streamLocked(name string, now time.Time) *stream {
	st, ok := s.streams[name]
	if !ok {
		st = &stream{nextID: 1, changed: make(chan struct{})}
		s.streams[name] = st
	}
	kept := st.events[:0]
	for _, e := range st.events {
		if e.expires.After(now) {
			kept = append(kept, e)
		} else if e.id > st.trimmedMax {
			st.trimmedMax = e.id
		}
	}
	st.events = kept
	return st
}

func (c *client) AppendEvent(_ context.Context, name string, data []byte, retention time.Duration) (int64, error) {
	if err := devutil.CheckName(name, "流名"); err != nil {
		return 0, err
	}
	if err := devutil.CheckPositive(retention, "retention"); err != nil {
		return 0, err
	}
	s, now, err := c.enter("AppendEvent")
	if err != nil {
		return 0, err
	}
	defer c.p.mu.Unlock()
	st := s.streamLocked(name, now)
	id := st.nextID
	st.nextID++
	st.events = append(st.events, event{id: id, data: append([]byte(nil), data...), expires: now.Add(retention)})
	close(st.changed)
	st.changed = make(chan struct{})
	return id, nil
}

func readLocked(st *stream, after int64, limit int) cache.EventPage {
	page := cache.EventPage{Gap: st.trimmedMax > after}
	for _, e := range st.events {
		if e.id > after && len(page.Events) < limit {
			page.Events = append(page.Events, cache.StreamEvent{ID: e.id, Data: append([]byte(nil), e.data...)})
		}
	}
	return page
}

func (c *client) ReadEvents(_ context.Context, name string, after int64, limit int) (cache.EventPage, error) {
	if err := devutil.CheckName(name, "流名"); err != nil {
		return cache.EventPage{}, err
	}
	if after < 0 || limit < 1 {
		return cache.EventPage{}, providererr.New(providererr.Invalid, "invalid_argument", "after 不能为负，limit 至少为 1")
	}
	s, now, err := c.enter("ReadEvents")
	if err != nil {
		return cache.EventPage{}, err
	}
	defer c.p.mu.Unlock()
	return readLocked(s.streamLocked(name, now), after, limit), nil
}

func (c *client) Subscribe(_ context.Context, name string, after int64) (cache.Subscription, error) {
	if err := devutil.CheckName(name, "流名"); err != nil {
		return nil, err
	}
	if after < 0 {
		return nil, providererr.New(providererr.Invalid, "invalid_argument", "after 不能为负")
	}
	if err := c.p.faults.Check("Subscribe"); err != nil {
		return nil, err
	}
	return &subscription{c: c, name: name, last: after, closed: make(chan struct{})}, nil
}

type subscription struct {
	c      *client
	name   string
	last   int64
	once   sync.Once
	closed chan struct{}
}

func (s *subscription) Next(ctx context.Context) (cache.StreamEvent, error) {
	for {
		s.c.p.mu.Lock()
		st := s.c.p.state(s.c.owner).streamLocked(s.name, s.c.p.clock.Now())
		page := readLocked(st, s.last, 1)
		changed := st.changed
		s.c.p.mu.Unlock()
		if len(page.Events) > 0 {
			s.last = page.Events[0].ID
			return page.Events[0], nil
		}
		select {
		case <-changed:
		case <-s.closed:
			return cache.StreamEvent{}, providererr.New(providererr.Invalid, "closed", "订阅已关闭")
		case <-ctx.Done():
			return cache.StreamEvent{}, providererr.Wrap(providererr.Transient, "timeout", "等待事件超时", ctx.Err())
		}
	}
}

func (s *subscription) Close() error {
	s.once.Do(func() { close(s.closed) })
	return nil
}
