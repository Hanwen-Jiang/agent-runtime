// Package cachetest 是 Cache 的一致性测试，与 Python 侧 agentruntime_core.conformance.cache 一一对应。
package cachetest

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/cache"
)

// Harness 由实现所在包提供。
type Harness interface {
	Provider() cache.Provider
	// Advance 让时间前进 d：开发实现拨动 FakeClock，生产实现真实等待。
	Advance(ctx context.Context, d time.Duration)
	conformance.Faultable
}

const (
	ttl  = 30 * time.Second
	long = time.Hour
)

// Run 跑完整的 Cache 一致性测试。
func Run(t *testing.T, newHarness func(t *testing.T) Harness) {
	probe := conformance.Probe[cache.Cache]{
		Operation: "Set",
		Write: func(ctx context.Context, c cache.Cache, name string) error {
			return c.Set(ctx, name, []byte("probe"), long)
		},
		Visible: func(ctx context.Context, c cache.Cache, name string) (bool, error) {
			v, ok, err := c.Get(ctx, name)
			return ok && string(v) == "probe", err
		},
	}
	binder := func(t *testing.T) conformance.Binder[cache.Cache] { return newHarness(t).Provider() }
	conformance.RunBindingRules(t, binder)
	conformance.RunIsolationRules(t, binder, probe)
	conformance.RunErrorRules(t, func(t *testing.T) (conformance.Binder[cache.Cache], conformance.Faultable) {
		h := newHarness(t)
		return h.Provider(), h
	}, probe)

	ctx := context.Background()
	setup := func(t *testing.T) (Harness, cache.Cache) {
		h := newHarness(t)
		return h, conformance.Must(h.Provider().Bind(conformance.TenantA()))
	}
	get := func(t *testing.T, c cache.Cache, key string) ([]byte, bool) {
		t.Helper()
		v, ok, err := c.Get(ctx, key)
		conformance.NoError(t, err)
		return v, ok
	}

	// --- 键值 ---
	conformance.Case(t, "GetMissingKey", func(t *testing.T) {
		_, c := setup(t)
		if _, ok := get(t, c, "missing"); ok {
			t.Fatal("missing key reported present")
		}
	})
	conformance.Case(t, "SetThenGetAndOverwrite", func(t *testing.T) {
		_, c := setup(t)
		conformance.NoError(t, c.Set(ctx, "k", []byte("\x00\xffv1"), ttl))
		if v, _ := get(t, c, "k"); string(v) != "\x00\xffv1" {
			t.Fatalf("got %q", v)
		}
		conformance.NoError(t, c.Set(ctx, "k", []byte("v2"), ttl))
		if v, _ := get(t, c, "k"); string(v) != "v2" {
			t.Fatalf("got %q", v)
		}
	})
	conformance.Case(t, "ValueExpiresAfterTTL", func(t *testing.T) {
		h, c := setup(t)
		conformance.NoError(t, c.Set(ctx, "k", []byte("v"), 2*time.Second))
		h.Advance(ctx, time.Second)
		if _, ok := get(t, c, "k"); !ok {
			t.Fatal("expired too early")
		}
		h.Advance(ctx, 2*time.Second)
		if _, ok := get(t, c, "k"); ok {
			t.Fatal("value did not expire")
		}
	})
	conformance.Case(t, "SetIfAbsentIsIdempotent", func(t *testing.T) {
		h, c := setup(t)
		if !conformance.Must(c.SetIfAbsent(ctx, "k", []byte("first"), time.Second)) {
			t.Fatal("first SetIfAbsent failed")
		}
		if conformance.Must(c.SetIfAbsent(ctx, "k", []byte("second"), ttl)) {
			t.Fatal("second SetIfAbsent overwrote")
		}
		if v, _ := get(t, c, "k"); string(v) != "first" {
			t.Fatalf("got %q", v)
		}
		h.Advance(ctx, 2*time.Second)
		if !conformance.Must(c.SetIfAbsent(ctx, "k", []byte("third"), ttl)) {
			t.Fatal("SetIfAbsent after expiry failed")
		}
	})
	conformance.Case(t, "DeleteReportsExistence", func(t *testing.T) {
		_, c := setup(t)
		conformance.NoError(t, c.Set(ctx, "k", []byte("v"), ttl))
		if !conformance.Must(c.Delete(ctx, "k")) || conformance.Must(c.Delete(ctx, "k")) {
			t.Fatal("Delete must report true then false")
		}
	})
	conformance.Case(t, "NonPositiveTTLIsInvalid", func(t *testing.T) {
		_, c := setup(t)
		for _, d := range []time.Duration{0, -time.Second} {
			conformance.ExpectError(t, c.Set(ctx, "k", []byte("v"), d), providererr.Invalid, "")
		}
	})
	conformance.Case(t, "MalformedKeyIsInvalid", func(t *testing.T) {
		_, c := setup(t)
		for _, key := range []string{"", "has space", "line\nbreak", strings.Repeat("x", 513)} {
			conformance.ExpectError(t, c.Set(ctx, key, []byte("v"), ttl), providererr.Invalid, "")
		}
	})

	// --- 计数与限流 ---
	conformance.Case(t, "IncrAccumulatesAtomically", func(t *testing.T) {
		_, c := setup(t)
		if n := conformance.Must(c.Incr(ctx, "n", 1, ttl)); n != 1 {
			t.Fatalf("first Incr = %d", n)
		}
		var wg sync.WaitGroup
		for range 49 {
			wg.Add(1)
			go func() { defer wg.Done(); _, _ = c.Incr(ctx, "n", 1, ttl) }()
		}
		wg.Wait()
		if n := conformance.Must(c.Incr(ctx, "n", 0, ttl)); n != 50 {
			t.Fatalf("after concurrent Incr = %d, want 50", n)
		}
	})
	conformance.Case(t, "CounterExpiresFromCreation", func(t *testing.T) {
		h, c := setup(t)
		conformance.Must(c.Incr(ctx, "n", 1, 10*time.Second))
		h.Advance(ctx, 6*time.Second)
		conformance.Must(c.Incr(ctx, "n", 1, 10*time.Second))
		h.Advance(ctx, 6*time.Second)
		if n := conformance.Must(c.Incr(ctx, "n", 1, 10*time.Second)); n != 1 {
			t.Fatalf("counter did not expire from creation: %d", n)
		}
	})
	conformance.Case(t, "RateLimitAllowsUpToLimit", func(t *testing.T) {
		_, c := setup(t)
		var allowed []bool
		var last cache.RateDecision
		for range 4 {
			last = conformance.Must(c.RateLimit(ctx, "api", 3, time.Minute, 1))
			allowed = append(allowed, last.Allowed)
		}
		if fmt.Sprint(allowed) != "[true true true false]" || last.RetryAfter <= 0 {
			t.Fatalf("allowed = %v, last = %+v", allowed, last)
		}
	})
	conformance.Case(t, "RejectedRequestsDoNotConsumeAndWindowSlides", func(t *testing.T) {
		h, c := setup(t)
		window := 10 * time.Second
		if !conformance.Must(c.RateLimit(ctx, "api", 2, window, 1)).Allowed {
			t.Fatal("first request rejected")
		}
		h.Advance(ctx, 6*time.Second)
		if !conformance.Must(c.RateLimit(ctx, "api", 2, window, 1)).Allowed {
			t.Fatal("second request rejected")
		}
		for range 5 {
			if conformance.Must(c.RateLimit(ctx, "api", 2, window, 1)).Allowed {
				t.Fatal("over-limit request allowed")
			}
		}
		h.Advance(ctx, 5*time.Second)
		if !conformance.Must(c.RateLimit(ctx, "api", 2, window, 1)).Allowed {
			t.Fatal("window did not slide")
		}
	})
	conformance.Case(t, "RateLimitRespectsCost", func(t *testing.T) {
		_, c := setup(t)
		got := []bool{
			conformance.Must(c.RateLimit(ctx, "tokens", 10, time.Minute, 7)).Allowed,
			conformance.Must(c.RateLimit(ctx, "tokens", 10, time.Minute, 4)).Allowed,
			conformance.Must(c.RateLimit(ctx, "tokens", 10, time.Minute, 3)).Allowed,
		}
		if fmt.Sprint(got) != "[true false true]" {
			t.Fatalf("got %v", got)
		}
	})
	conformance.Case(t, "RateLimitIsExactUnderConcurrency", func(t *testing.T) {
		_, c := setup(t)
		var mu sync.Mutex
		var wg sync.WaitGroup
		allowed := 0
		for range 20 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				if d, err := c.RateLimit(ctx, "burst", 5, time.Minute, 1); err == nil && d.Allowed {
					mu.Lock()
					allowed++
					mu.Unlock()
				}
			}()
		}
		wg.Wait()
		if allowed != 5 {
			t.Fatalf("allowed = %d, want 5", allowed)
		}
	})
	conformance.Case(t, "RateLimitRejectsBadArguments", func(t *testing.T) {
		_, c := setup(t)
		_, err := c.RateLimit(ctx, "api", 0, time.Second, 1)
		conformance.ExpectError(t, err, providererr.Invalid, "")
		_, err = c.RateLimit(ctx, "api", 5, time.Second, 0)
		conformance.ExpectError(t, err, providererr.Invalid, "")
	})
	conformance.Case(t, "CountersAndLocksAreIsolatedPerTenant", func(t *testing.T) {
		h := newHarness(t)
		a := conformance.Must(h.Provider().Bind(conformance.TenantA()))
		b := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		conformance.Must(a.Incr(ctx, "n", 10, ttl))
		if n := conformance.Must(b.Incr(ctx, "n", 1, ttl)); n != 1 {
			t.Fatalf("tenant B counter = %d", n)
		}
		if conformance.Must(a.AcquireLock(ctx, "job", ttl)) == nil || conformance.Must(b.AcquireLock(ctx, "job", ttl)) == nil {
			t.Fatal("locks must be independent per tenant")
		}
	})

	// --- 锁 ---
	conformance.Case(t, "LockIsExclusive", func(t *testing.T) {
		_, c := setup(t)
		lock := conformance.Must(c.AcquireLock(ctx, "job", ttl))
		if lock == nil || conformance.Must(c.AcquireLock(ctx, "job", ttl)) != nil {
			t.Fatal("lock not exclusive")
		}
		if !conformance.Must(c.ReleaseLock(ctx, *lock)) || conformance.Must(c.ReleaseLock(ctx, *lock)) {
			t.Fatal("ReleaseLock must report true then false")
		}
		if conformance.Must(c.AcquireLock(ctx, "job", ttl)) == nil {
			t.Fatal("released lock not re-acquirable")
		}
	})
	conformance.Case(t, "LockIsExclusiveUnderConcurrency", func(t *testing.T) {
		_, c := setup(t)
		var mu sync.Mutex
		var wg sync.WaitGroup
		holders := 0
		for range 20 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				if l, err := c.AcquireLock(ctx, "job", ttl); err == nil && l != nil {
					mu.Lock()
					holders++
					mu.Unlock()
				}
			}()
		}
		wg.Wait()
		if holders != 1 {
			t.Fatalf("holders = %d, want 1", holders)
		}
	})
	conformance.Case(t, "ExpiredLockIsFenced", func(t *testing.T) {
		h, c := setup(t)
		old := conformance.Must(c.AcquireLock(ctx, "job", 5*time.Second))
		h.Advance(ctx, 6*time.Second)
		fresh := conformance.Must(c.AcquireLock(ctx, "job", ttl))
		if fresh == nil || fresh.Token == old.Token {
			t.Fatal("expired lock not re-acquired with a new token")
		}
		if conformance.Must(c.ReleaseLock(ctx, *old)) {
			t.Fatal("stale holder released the new lock")
		}
		if conformance.Must(c.ExtendLock(ctx, *old, ttl)) != nil {
			t.Fatal("stale holder extended the new lock")
		}
		if conformance.Must(c.AcquireLock(ctx, "job", ttl)) != nil {
			t.Fatal("new holder lost its lock")
		}
	})
	conformance.Case(t, "ExtendLockProlongsIt", func(t *testing.T) {
		h, c := setup(t)
		lock := conformance.Must(c.AcquireLock(ctx, "job", 5*time.Second))
		h.Advance(ctx, 4*time.Second)
		if conformance.Must(c.ExtendLock(ctx, *lock, 5*time.Second)) == nil {
			t.Fatal("extend failed")
		}
		h.Advance(ctx, 4*time.Second)
		if conformance.Must(c.AcquireLock(ctx, "job", ttl)) != nil {
			t.Fatal("extended lock was lost")
		}
	})

	// --- 推流 ---
	conformance.Case(t, "EventIDsIncreaseAndResume", func(t *testing.T) {
		_, c := setup(t)
		var ids []int64
		for i := range 5 {
			ids = append(ids, conformance.Must(c.AppendEvent(ctx, "task-1", []byte(fmt.Sprintf("e%d", i)), long)))
		}
		for i := 1; i < len(ids); i++ {
			if ids[i] <= ids[i-1] || ids[0] < 1 {
				t.Fatalf("ids not strictly increasing from >= 1: %v", ids)
			}
		}
		page := conformance.Must(c.ReadEvents(ctx, "task-1", ids[1], 100))
		if len(page.Events) != 3 || string(page.Events[0].Data) != "e2" || page.Gap {
			t.Fatalf("page = %+v", page)
		}
		page = conformance.Must(c.ReadEvents(ctx, "task-1", 0, 2))
		if len(page.Events) != 2 || string(page.Events[1].Data) != "e1" {
			t.Fatalf("limit not honoured: %+v", page)
		}
	})
	conformance.Case(t, "ExpiredEventsAreReportedAsGap", func(t *testing.T) {
		h, c := setup(t)
		first := conformance.Must(c.AppendEvent(ctx, "task-1", []byte("old"), 5*time.Second))
		h.Advance(ctx, 6*time.Second)
		second := conformance.Must(c.AppendEvent(ctx, "task-1", []byte("new"), 5*time.Second))
		page := conformance.Must(c.ReadEvents(ctx, "task-1", 0, 100))
		if len(page.Events) != 1 || page.Events[0].ID != second || !page.Gap {
			t.Fatalf("page = %+v", page)
		}
		if conformance.Must(c.ReadEvents(ctx, "task-1", first, 100)).Gap {
			t.Fatal("no gap expected after the expired event")
		}
	})
	conformance.Case(t, "SubscribeReplaysThenStreamsLive", func(t *testing.T) {
		_, c := setup(t)
		first := conformance.Must(c.AppendEvent(ctx, "task-1", []byte("a"), long))
		conformance.Must(c.AppendEvent(ctx, "task-1", []byte("b"), long))
		sub := conformance.Must(c.Subscribe(ctx, "task-1", first))
		defer sub.Close()
		go func() {
			time.Sleep(50 * time.Millisecond)
			_, _ = c.AppendEvent(ctx, "task-1", []byte("c"), long)
		}()
		waitCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		var got []string
		for len(got) < 2 {
			event := conformance.Must(sub.Next(waitCtx))
			got = append(got, string(event.Data))
		}
		if fmt.Sprint(got) != "[b c]" {
			t.Fatalf("got %v", got)
		}
	})
	conformance.Case(t, "StreamsAreIsolatedPerTenant", func(t *testing.T) {
		h := newHarness(t)
		a := conformance.Must(h.Provider().Bind(conformance.TenantA()))
		b := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		conformance.Must(a.AppendEvent(ctx, "task-1", []byte("secret-progress"), long))
		if page := conformance.Must(b.ReadEvents(ctx, "task-1", 0, 100)); len(page.Events) != 0 {
			t.Fatalf("tenant B read %d events of tenant A", len(page.Events))
		}
	})
}
