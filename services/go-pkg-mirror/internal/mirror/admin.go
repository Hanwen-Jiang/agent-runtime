package mirror

import (
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// --- 拉黑 ---

// Entry 是拉黑的一项：生态、包名、版本（版本为空表示这个包的全部版本）。
type Entry struct {
	Ecosystem string    `json:"ecosystem"`
	Name      string    `json:"name"`
	Version   string    `json:"version"`
	Reason    string    `json:"reason,omitempty"`
	AddedAt   time.Time `json:"added_at"`
}

// Denylist 是拉黑的版本，写在一个 JSON 文件里（path 为空时只在内存里）。
type Denylist struct {
	path    string
	mu      sync.RWMutex
	entries []Entry
}

// NewDenylist 读回（或新建）拉黑名单。
func NewDenylist(path string) *Denylist {
	d := &Denylist{path: path}
	if path != "" {
		if data, err := os.ReadFile(path); err == nil {
			_ = json.Unmarshal(data, &d.entries)
		}
	}
	return d
}

func normalizeName(ecosystem, name string) string {
	switch ecosystem {
	case "pypi":
		return normalizePyPI(name)
	case "npm":
		return strings.ToLower(name)
	default:
		return name
	}
}

// Contains 判断一个版本是不是被拉黑了。
func (d *Denylist) Contains(ecosystem, name, version string) bool {
	d.mu.RLock()
	defer d.mu.RUnlock()
	n := normalizeName(ecosystem, name)
	for _, e := range d.entries {
		if e.Ecosystem == ecosystem && normalizeName(ecosystem, e.Name) == n && (e.Version == "" || e.Version == version) {
			return true
		}
	}
	return false
}

// Add 拉黑一项并写回文件。
func (d *Denylist) Add(e Entry) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.entries = append(d.entries, e)
	if d.path == "" {
		return nil
	}
	data, err := json.MarshalIndent(d.entries, "", "  ")
	if err != nil {
		return err
	}
	tmp := d.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, d.path)
}

// List 是当前的名单。
func (d *Denylist) List() []Entry {
	d.mu.RLock()
	defer d.mu.RUnlock()
	return append([]Entry(nil), d.entries...)
}

// --- 指标 ---

// Metrics 是计数（Prometheus 的文本格式从 /metrics 出）。
type Metrics struct {
	mu sync.Mutex
	c  map[[2]string]int64
}

// NewMetrics 建一组计数。
func NewMetrics() *Metrics { return &Metrics{c: map[[2]string]int64{}} }

func (m *Metrics) add(name, label string, n int64) {
	m.mu.Lock()
	m.c[[2]string{name, label}] += n
	m.mu.Unlock()
}

func (m *Metrics) inc(name, label string) { m.add(name, label, 1) }

// Get 是一个计数的当前值（测试用）。
func (m *Metrics) Get(name, label string) int64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.c[[2]string{name, label}]
}

// --- 管理接口（内部端口） ---

// Admin 是管理接口：清除并拉黑一个版本、看名单、指标。要 Authorization: Bearer <管理令牌>（SecretStore 平台作用域的
// pkg-mirror/admin-token），指标除外（集群内部抓）。
func (s *Server) Admin(token string) http.Handler {
	mux := http.NewServeMux()
	authed := func(h http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if token == "" || subtle.ConstantTimeCompare([]byte(got), []byte(token)) != 1 {
				problem(w, http.StatusUnauthorized, "unauthorized", "要管理令牌")
				return
			}
			h(w, r)
		}
	}
	mux.HandleFunc("POST /admin/purge", authed(s.purge))
	mux.HandleFunc("GET /admin/denylist", authed(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(s.cfg.Deny.List())
	}))
	mux.HandleFunc("GET /metrics", s.metrics)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") })
	return mux
}

func (s *Server) purge(w http.ResponseWriter, r *http.Request) {
	var e Entry
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&e); err != nil || e.Name == "" {
		problem(w, http.StatusBadRequest, "invalid_request", "要 {ecosystem, name, version}")
		return
	}
	if e.Ecosystem != "pypi" && e.Ecosystem != "npm" && e.Ecosystem != "go" {
		problem(w, http.StatusBadRequest, "invalid_ecosystem", "生态只能是 pypi、npm、go")
		return
	}
	e.Name = normalizeName(e.Ecosystem, e.Name)
	e.AddedAt = s.cfg.Clock().UTC()
	if err := s.cfg.Deny.Add(e); err != nil {
		problem(w, http.StatusInternalServerError, "denylist_write_failed", "拉黑名单写不进去")
		return
	}
	removed := s.cfg.Blobs.Purge(e.Ecosystem, e.Name, e.Version)
	// 元数据里还列着它：删掉缓存，下次重新取并按名单过滤。
	switch e.Ecosystem {
	case "pypi":
		s.cfg.Meta.Delete("pypi:index:" + e.Name)
	case "npm":
		s.cfg.Meta.Delete("npm:" + e.Name)
	case "go":
		s.cfg.Meta.Delete("go:list:" + escapeGo(e.Name))
		s.cfg.Meta.Delete("go:latest:" + escapeGo(e.Name))
	}
	s.cfg.Log.Warn("拉黑并清除", "ecosystem", e.Ecosystem, "name", e.Name, "version", e.Version, "files", removed)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"purged_files": removed, "entry": e})
}

func escapeGo(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= 'A' && r <= 'Z' {
			b.WriteByte('!')
			b.WriteRune(r - 'A' + 'a')
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}

func (s *Server) metrics(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	st := s.cfg.Blobs.Stats()
	var lines []string
	s.Metrics.mu.Lock()
	for k, v := range s.Metrics.c {
		lines = append(lines, fmt.Sprintf("agentruntime_pkg_mirror_%s_total{label=%q} %d", k[0], k[1], v))
	}
	s.Metrics.mu.Unlock()
	sort.Strings(lines)
	for seg, used := range st.BySegment {
		lines = append(lines, fmt.Sprintf("agentruntime_pkg_mirror_blob_bytes{segment=%q} %d", seg, used))
	}
	lines = append(lines,
		fmt.Sprintf("agentruntime_pkg_mirror_blob_files %d", st.Files),
		fmt.Sprintf("agentruntime_pkg_mirror_evicted_total %d", st.Evicted),
		fmt.Sprintf("agentruntime_pkg_mirror_corrupt_total %d", st.Corrupt),
		fmt.Sprintf("agentruntime_pkg_mirror_upstream_mismatch_total %d", st.Mismatch),
		fmt.Sprintf("agentruntime_pkg_mirror_meta_bytes %d", s.cfg.Meta.Used()),
	)
	if snap, ok := s.cfg.Usage.(interface {
		Snapshot(time.Time) map[string]int64
	}); ok {
		for tenant, n := range snap.Snapshot(s.cfg.Clock()) {
			lines = append(lines, fmt.Sprintf("agentruntime_pkg_mirror_tenant_miss_bytes{tenant_id=%q} %d", tenant, n))
		}
	}
	_, _ = io.WriteString(w, strings.Join(lines, "\n")+"\n")
}
