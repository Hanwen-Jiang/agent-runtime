package crawler

import (
	"slices"
	"strings"
	"sync"
	"time"
)

type domainState struct {
	failures  int
	openUntil time.Time
}

// CircuitBreaker trips per external domain after consecutive upstream errors or 5xx responses.
type CircuitBreaker struct {
	threshold int
	cooldown  time.Duration
	now       func() time.Time

	mu     sync.Mutex
	states map[string]*domainState
}

func NewCircuitBreaker(threshold int, cooldown time.Duration, now func() time.Time) *CircuitBreaker {
	if threshold <= 0 {
		threshold = 5
	}
	if cooldown <= 0 {
		cooldown = 120 * time.Second
	}
	if now == nil {
		now = time.Now
	}
	return &CircuitBreaker{
		threshold: threshold,
		cooldown:  cooldown,
		now:       now,
		states:    map[string]*domainState{},
	}
}

func normalizeCBKey(domain string) string {
	return strings.TrimSpace(strings.ToLower(domain))
}

// Allow reports whether a request to domain may proceed under the current circuit state.
func (c *CircuitBreaker) Allow(domain string) bool {
	key := normalizeCBKey(domain)
	if key == "" {
		return true
	}
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	st, ok := c.states[key]
	if !ok || st.openUntil.IsZero() {
		return true
	}
	if now.Before(st.openUntil) {
		return false
	}
	// Cooldown elapsed: allow half-open probe and reset window.
	st.openUntil = time.Time{}
	st.failures = 0
	return true
}

// RecordSuccess clears failure state for domain.
func (c *CircuitBreaker) RecordSuccess(domain string) {
	key := normalizeCBKey(domain)
	if key == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.states, key)
}

// RecordFailure increments failure count for domain and returns true if the circuit opens.
func (c *CircuitBreaker) RecordFailure(domain string) bool {
	key := normalizeCBKey(domain)
	if key == "" {
		return false
	}
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	st, ok := c.states[key]
	if !ok {
		st = &domainState{}
		c.states[key] = st
	}
	st.failures++
	if st.failures >= c.threshold {
		st.openUntil = now.Add(c.cooldown)
		st.failures = 0
		return true
	}
	return false
}

// OpenDomains returns the sorted list of domains whose circuit is currently open.
func (c *CircuitBreaker) OpenDomains() []string {
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	var out []string
	for domain, st := range c.states {
		if !st.openUntil.IsZero() && now.Before(st.openUntil) {
			out = append(out, domain)
		}
	}
	slices.Sort(out)
	return out
}
