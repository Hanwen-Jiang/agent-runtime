package crawler

import (
	"strings"
	"sync"
	"time"
)

// AuditEntry captures metadata for a single crawler request without logging query secrets or bodies.
type AuditEntry struct {
	Timestamp  string `json:"timestamp"`
	Actor      string `json:"actor"`
	Kind       string `json:"kind"`
	Zone       string `json:"zone,omitempty"`
	Domain     string `json:"domain,omitempty"`
	Status     int    `json:"status"`
	Cached     bool   `json:"cached"`
	RenderedJS bool   `json:"rendered_js,omitempty"`
	Blocked    bool   `json:"blocked,omitempty"`
	LatencyMS  int64  `json:"latency_ms"`
	Reason     string `json:"reason,omitempty"`
}

// AuditLog maintains a bounded in-memory ring buffer of crawler requests.
type AuditLog struct {
	max     int
	mu      sync.Mutex
	entries []AuditEntry
}

func NewAuditLog(maxEntries int) *AuditLog {
	if maxEntries <= 0 {
		maxEntries = 500
	}
	return &AuditLog{
		max:     maxEntries,
		entries: make([]AuditEntry, 0, min(maxEntries, 64)),
	}
}

func (a *AuditLog) Record(entry AuditEntry) {
	if entry.Timestamp == "" {
		entry.Timestamp = time.Now().UTC().Format(time.RFC3339Nano)
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.entries) >= a.max {
		copy(a.entries, a.entries[1:])
		a.entries[len(a.entries)-1] = entry
		return
	}
	a.entries = append(a.entries, entry)
}

// Query returns newest-first audit entries matching optional domain and blocked-only filters.
func (a *AuditLog) Query(limit int, domain string, blockedOnly bool) []AuditEntry {
	if limit <= 0 || limit > a.max {
		limit = 50
	}
	wantDomain := strings.TrimSpace(strings.ToLower(domain))
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]AuditEntry, 0, min(limit, len(a.entries)))
	for i := len(a.entries) - 1; i >= 0 && len(out) < limit; i-- {
		e := a.entries[i]
		if wantDomain != "" && strings.ToLower(e.Domain) != wantDomain {
			continue
		}
		if blockedOnly && !e.Blocked {
			continue
		}
		out = append(out, e)
	}
	return out
}
