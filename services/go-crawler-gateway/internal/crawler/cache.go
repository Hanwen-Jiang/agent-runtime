package crawler

import (
	"sync"
	"time"
)

type cacheEntry[V any] struct {
	val       V
	expiresAt time.Time
}

// TTLCache provides bounded in-memory caching for search and fetch responses.
type TTLCache[V any] struct {
	mu      sync.Mutex
	maxSize int
	ttl     time.Duration
	now     func() time.Time
	items   map[string]cacheEntry[V]
}

func NewTTLCache[V any](maxSize int, ttl time.Duration, now func() time.Time) *TTLCache[V] {
	if maxSize <= 0 {
		maxSize = 512
	}
	if ttl <= 0 {
		ttl = 5 * time.Minute
	}
	if now == nil {
		now = time.Now
	}
	return &TTLCache[V]{
		maxSize: maxSize,
		ttl:     ttl,
		now:     now,
		items:   map[string]cacheEntry[V]{},
	}
}

func (c *TTLCache[V]) Get(key string) (V, bool) {
	var zero V
	if c == nil {
		return zero, false
	}
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	ent, ok := c.items[key]
	if !ok {
		return zero, false
	}
	if now.After(ent.expiresAt) {
		delete(c.items, key)
		return zero, false
	}
	return ent.val, true
}

func (c *TTLCache[V]) Set(key string, val V) {
	if c == nil {
		return
	}
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.items) >= c.maxSize {
		for k, v := range c.items {
			if now.After(v.expiresAt) {
				delete(c.items, k)
			}
		}
		if len(c.items) >= c.maxSize {
			for k := range c.items {
				delete(c.items, k)
				break
			}
		}
	}
	c.items[key] = cacheEntry[V]{val: val, expiresAt: now.Add(c.ttl)}
}

func (c *TTLCache[V]) Len() int {
	if c == nil {
		return 0
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.items)
}
