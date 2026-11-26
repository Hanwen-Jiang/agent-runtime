package crawler

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type ServerConfig struct {
	Token       string
	AdminToken  string
	RPM         int
	CacheTTL    time.Duration
	CacheSize   int
	AuditSize   int
	CBThreshold int
	CBCooldown  time.Duration
	Timeout     time.Duration
	Policy      *Policy
	Backend     *SearchBackend
	Now         func() time.Time
}

type Metrics struct {
	SearchRequests   atomic.Int64
	FetchRequests    atomic.Int64
	ExternalFetches  atomic.Int64
	InternalFetches  atomic.Int64
	CacheHits        atomic.Int64
	BlockedRequests  atomic.Int64
	UpstreamErrors   atomic.Int64
	CircuitOpenTotal atomic.Int64
}

func (m *Metrics) Snapshot() map[string]int64 {
	return map[string]int64{
		"search_requests":    m.SearchRequests.Load(),
		"fetch_requests":     m.FetchRequests.Load(),
		"external_fetches":   m.ExternalFetches.Load(),
		"internal_fetches":   m.InternalFetches.Load(),
		"cache_hits":         m.CacheHits.Load(),
		"blocked_requests":   m.BlockedRequests.Load(),
		"upstream_errors":    m.UpstreamErrors.Load(),
		"circuit_open_total": m.CircuitOpenTotal.Load(),
	}
}

type Server struct {
	cfg         ServerConfig
	searchCache *TTLCache[map[string]any]
	fetchCache  *TTLCache[map[string]any]
	circuits    *CircuitBreaker
	audit       *AuditLog
	metrics     Metrics
	rateMu      sync.Mutex
	rateHits    map[string][]time.Time
}

func NewServer(cfg ServerConfig) *Server {
	if cfg.RPM <= 0 {
		cfg.RPM = 60
	}
	if cfg.CacheTTL <= 0 {
		cfg.CacheTTL = 5 * time.Minute
	}
	if cfg.CacheSize <= 0 {
		cfg.CacheSize = 512
	}
	if cfg.AuditSize <= 0 {
		cfg.AuditSize = 500
	}
	if cfg.CBThreshold <= 0 {
		cfg.CBThreshold = 5
	}
	if cfg.CBCooldown <= 0 {
		cfg.CBCooldown = 120 * time.Second
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 15 * time.Second
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Policy == nil {
		cfg.Policy = NewPolicy(PolicyConfig{})
	}
	if cfg.Backend == nil {
		cfg.Backend = NewSearchBackend(SearchConfig{Timeout: cfg.Timeout})
	}
	return &Server{
		cfg:         cfg,
		searchCache: NewTTLCache[map[string]any](cfg.CacheSize, cfg.CacheTTL, cfg.Now),
		fetchCache:  NewTTLCache[map[string]any](cfg.CacheSize, cfg.CacheTTL, cfg.Now),
		circuits:    NewCircuitBreaker(cfg.CBThreshold, cfg.CBCooldown, cfg.Now),
		audit:       NewAuditLog(cfg.AuditSize),
		rateHits:    map[string][]time.Time{},
	}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /v1/status", s.handleStatus)
	mux.HandleFunc("GET /metrics", s.handleMetrics)
	mux.HandleFunc("GET /v1/audit", s.withAuthAndLimit(s.handleAudit))
	mux.HandleFunc("GET /v1/policy/internal-allowlist", s.withAuthAndLimit(s.handleListInternalAllowlist))
	mux.HandleFunc("POST /v1/policy/internal-allowlist/propose", s.withAdminAuth(s.handleProposeInternalAllowlist))
	mux.HandleFunc("POST /v1/policy/internal-allowlist/approve", s.withAdminAuth(s.handleApproveInternalAllowlist))
	mux.HandleFunc("POST /v1/search", s.withAuthAndLimit(s.handleSearch))
	mux.HandleFunc("POST /v1/fetch", s.withAuthAndLimit(s.handleFetch))
	return mux
}

func requestActor(r *http.Request) string {
	actor := strings.TrimSpace(r.Header.Get("X-Forwarded-For"))
	if actor == "" {
		host, _, err := net.SplitHostPort(r.RemoteAddr)
		if err == nil {
			actor = host
		} else {
			actor = r.RemoteAddr
		}
	}
	return actor
}

func (s *Server) withAdminAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		want := strings.TrimSpace(s.cfg.AdminToken)
		if want == "" {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "crawler admin governance disabled: AR_CRAWLER_ADMIN_TOKEN not configured"})
			return
		}
		got, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
		if !ok || subtle.ConstantTimeCompare([]byte(strings.TrimSpace(got)), []byte(want)) != 1 {
			writeJSON(w, http.StatusForbidden, map[string]string{"error": "admin bearer token required"})
			return
		}
		next(w, r)
	}
}

func (s *Server) withAuthAndLimit(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if want := strings.TrimSpace(s.cfg.Token); want != "" {
			got, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
			if !ok || subtle.ConstantTimeCompare([]byte(strings.TrimSpace(got)), []byte(want)) != 1 {
				writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "missing or invalid crawler bearer token"})
				return
			}
		}
		actor := r.Header.Get("X-Forwarded-For")
		if actor == "" {
			host, _, err := net.SplitHostPort(r.RemoteAddr)
			if err == nil {
				actor = host
			} else {
				actor = r.RemoteAddr
			}
		}
		if !s.allowRate(actor) {
			writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": "crawler rate limit exceeded"})
			return
		}
		next(w, r)
	}
}

func (s *Server) allowRate(actor string) bool {
	now := s.cfg.Now()
	cutoff := now.Add(-time.Minute)
	s.rateMu.Lock()
	defer s.rateMu.Unlock()
	var kept []time.Time
	for _, ts := range s.rateHits[actor] {
		if ts.After(cutoff) {
			kept = append(kept, ts)
		}
	}
	if len(kept) >= s.cfg.RPM {
		s.rateHits[actor] = kept
		return false
	}
	s.rateHits[actor] = append(kept, now)
	return true
}

func (s *Server) handleStatus(w http.ResponseWriter, _ *http.Request) {
	openCircuits := s.circuits.OpenDomains()
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":                   true,
		"service":              "crawler-gateway",
		"upstream_search":      s.cfg.Backend.ProviderName(),
		"headless_configured":  s.cfg.Backend.HeadlessConfigured(),
		"internal_allow_count": s.cfg.Policy.InternalAllowCount(),
		"open_circuits":        openCircuits,
		"cache_entries":        s.searchCache.Len() + s.fetchCache.Len(),
		"metrics":              s.metrics.Snapshot(),
	})
}

func (s *Server) handleMetrics(w http.ResponseWriter, _ *http.Request) {
	snap := s.metrics.Snapshot()
	openCount := len(s.circuits.OpenDomains())
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	fmt.Fprintf(w, "# TYPE crawler_up gauge\ncrawler_up 1\n")
	fmt.Fprintf(w, "# TYPE crawler_requests_total counter\ncrawler_requests_total{kind=\"search\"} %d\ncrawler_requests_total{kind=\"fetch\"} %d\n", snap["search_requests"], snap["fetch_requests"])
	fmt.Fprintf(w, "# TYPE crawler_cache_hits_total counter\ncrawler_cache_hits_total %d\n", snap["cache_hits"])
	fmt.Fprintf(w, "# TYPE crawler_blocked_total counter\ncrawler_blocked_total %d\n", snap["blocked_requests"])
	fmt.Fprintf(w, "# TYPE crawler_upstream_errors_total counter\ncrawler_upstream_errors_total %d\n", snap["upstream_errors"])
	fmt.Fprintf(w, "# TYPE crawler_circuit_open_total counter\ncrawler_circuit_open_total %d\n", snap["circuit_open_total"])
	fmt.Fprintf(w, "# TYPE crawler_circuits_open gauge\ncrawler_circuits_open %d\n", openCount)
	fmt.Fprintf(w, "# TYPE crawler_cache_entries gauge\ncrawler_cache_entries %d\n", s.searchCache.Len()+s.fetchCache.Len())
}

func (s *Server) handleAudit(w http.ResponseWriter, r *http.Request) {
	limit := 50
	if raw := strings.TrimSpace(r.URL.Query().Get("limit")); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 {
			limit = n
		}
	}
	domain := r.URL.Query().Get("domain")
	blockedOnly := r.URL.Query().Get("blocked") == "true" || r.URL.Query().Get("blocked") == "1"
	entries := s.audit.Query(limit, domain, blockedOnly)
	writeJSON(w, http.StatusOK, map[string]any{
		"entries": entries,
		"count":   len(entries),
	})
}

func (s *Server) handleListInternalAllowlist(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"allowlist": s.cfg.Policy.InternalAllowlist(),
		"proposals": s.cfg.Policy.InternalProposals(),
	})
}

func (s *Server) handleProposeInternalAllowlist(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Action string `json:"action"`
		Target string `json:"target"`
		Reason string `json:"reason"`
		Actor  string `json:"actor"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON payload"})
		return
	}
	actor := strings.TrimSpace(req.Actor)
	if actor == "" {
		actor = requestActor(r)
	}
	prop, err := s.cfg.Policy.ProposeInternalAllow(req.Action, req.Target, req.Reason, actor)
	if err != nil {
		status := http.StatusBadRequest
		if errors.Is(err, ErrBlocked) {
			status = http.StatusForbidden
		}
		writeJSON(w, status, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, prop)
}

func (s *Server) handleApproveInternalAllowlist(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ID    string `json:"id"`
		Actor string `json:"actor"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON payload"})
		return
	}
	actor := strings.TrimSpace(req.Actor)
	if actor == "" {
		actor = requestActor(r)
	}
	prop, err := s.cfg.Policy.ApproveInternalAllow(req.ID, actor)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	s.audit.Record(AuditEntry{
		Timestamp: s.cfg.Now().UTC().Format(time.RFC3339Nano),
		Actor:     actor,
		Kind:      "policy_approve",
		Zone:      ZoneInternal,
		Domain:    prop.Target,
		Status:    http.StatusOK,
		Reason:    prop.Action + ":" + prop.ID,
	})
	writeJSON(w, http.StatusOK, map[string]any{
		"proposal":  prop,
		"allowlist": s.cfg.Policy.InternalAllowlist(),
	})
}

func (s *Server) handleSearch(w http.ResponseWriter, r *http.Request) {
	started := s.cfg.Now()
	actor := requestActor(r)
	s.metrics.SearchRequests.Add(1)
	var req struct {
		Query          string   `json:"query"`
		MaxResults     int      `json:"max_results"`
		IncludeDomains []string `json:"include_domains"`
		SearchDepth    string   `json:"search_depth"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON payload"})
		return
	}
	req.Query = strings.TrimSpace(req.Query)
	if req.Query == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "query is required"})
		return
	}
	if req.MaxResults <= 0 || req.MaxResults > 10 {
		req.MaxResults = 5
	}
	if req.SearchDepth == "" {
		req.SearchDepth = "basic"
	}

	cacheKey := fmt.Sprintf("s|%s|%d|%s|%s", req.Query, req.MaxResults, strings.Join(req.IncludeDomains, ","), req.SearchDepth)
	if hit, ok := s.searchCache.Get(cacheKey); ok {
		s.metrics.CacheHits.Add(1)
		cloned := cloneMap(hit)
		cloned["cached"] = true
		s.audit.Record(AuditEntry{
			Timestamp: s.cfg.Now().UTC().Format(time.RFC3339Nano),
			Actor:     actor,
			Kind:      "search",
			Domain:    s.cfg.Backend.ProviderName(),
			Status:    http.StatusOK,
			Cached:    true,
			LatencyMS: s.cfg.Now().Sub(started).Milliseconds(),
		})
		writeJSON(w, http.StatusOK, cloned)
		return
	}

	results, err := s.cfg.Backend.Search(r.Context(), req.Query, req.MaxResults, req.IncludeDomains, req.SearchDepth)
	if err != nil {
		s.metrics.UpstreamErrors.Add(1)
		s.audit.Record(AuditEntry{
			Timestamp: s.cfg.Now().UTC().Format(time.RFC3339Nano),
			Actor:     actor,
			Kind:      "search",
			Domain:    s.cfg.Backend.ProviderName(),
			Status:    http.StatusBadGateway,
			LatencyMS: s.cfg.Now().Sub(started).Milliseconds(),
			Reason:    "search_upstream_error",
		})
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("search upstream failed: %v", err)})
		return
	}
	if len(results) > req.MaxResults {
		results = results[:req.MaxResults]
	}
	resp := map[string]any{
		"query":    req.Query,
		"provider": s.cfg.Backend.ProviderName(),
		"cached":   false,
		"results":  results,
		"total":    len(results),
	}
	s.searchCache.Set(cacheKey, cloneMap(resp))
	s.audit.Record(AuditEntry{
		Timestamp: s.cfg.Now().UTC().Format(time.RFC3339Nano),
		Actor:     actor,
		Kind:      "search",
		Domain:    s.cfg.Backend.ProviderName(),
		Status:    http.StatusOK,
		LatencyMS: s.cfg.Now().Sub(started).Milliseconds(),
	})
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleFetch(w http.ResponseWriter, r *http.Request) {
	started := s.cfg.Now()
	actor := requestActor(r)
	s.metrics.FetchRequests.Add(1)
	var req struct {
		URL             string `json:"url"`
		Zone            string `json:"zone"`
		MaxBytes        int64  `json:"max_bytes"`
		ExtractReadable *bool  `json:"extract_readable"`
		FollowRedirects *bool  `json:"follow_redirects"`
		RenderJS        bool   `json:"render_js"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON payload"})
		return
	}
	req.URL = strings.TrimSpace(req.URL)
	if req.URL == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "url is required"})
		return
	}
	if req.Zone == "" {
		req.Zone = ZoneExternal
	}
	if req.Zone == ZoneInternal {
		s.metrics.InternalFetches.Add(1)
	} else {
		s.metrics.ExternalFetches.Add(1)
	}

	rawDomain := ""
	if pu, perr := url.Parse(req.URL); perr == nil {
		rawDomain = strings.ToLower(pu.Hostname())
	}

	readable := true
	if req.ExtractReadable != nil {
		readable = *req.ExtractReadable
	}
	follow := true
	if req.FollowRedirects != nil {
		follow = *req.FollowRedirects
	}
	if req.MaxBytes <= 0 || req.MaxBytes > 1_000_000 {
		req.MaxBytes = 512_000
	}

	// Validate zone/URL policy BEFORE hitting cache so blocked targets never leak cached entries.
	parsed, _, _, err := s.cfg.Policy.ValidateTarget(r.Context(), req.URL, req.Zone)
	if err != nil {
		if errors.Is(err, ErrBlocked) {
			s.metrics.BlockedRequests.Add(1)
			s.audit.Record(AuditEntry{
				Timestamp: s.cfg.Now().UTC().Format(time.RFC3339Nano),
				Actor:     actor,
				Kind:      "fetch",
				Zone:      req.Zone,
				Domain:    rawDomain,
				Status:    http.StatusForbidden,
				Blocked:   true,
				LatencyMS: s.cfg.Now().Sub(started).Milliseconds(),
				Reason:    "policy_blocked",
			})
			writeJSON(w, http.StatusForbidden, map[string]string{"error": err.Error()})
			return
		}
		if errors.Is(err, ErrInvalid) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		s.metrics.UpstreamErrors.Add(1)
		s.audit.Record(AuditEntry{
			Timestamp: s.cfg.Now().UTC().Format(time.RFC3339Nano),
			Actor:     actor,
			Kind:      "fetch",
			Zone:      req.Zone,
			Domain:    rawDomain,
			Status:    http.StatusBadGateway,
			LatencyMS: s.cfg.Now().Sub(started).Milliseconds(),
			Reason:    "dns_or_validate_error",
		})
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	domain := strings.ToLower(parsed.Hostname())

	cacheKey := fmt.Sprintf("f|%s|%s|%d|%v|%v|%v", req.Zone, req.URL, req.MaxBytes, readable, follow, req.RenderJS)
	if hit, ok := s.fetchCache.Get(cacheKey); ok {
		s.metrics.CacheHits.Add(1)
		cloned := cloneMap(hit)
		cloned["cached"] = true
		s.audit.Record(AuditEntry{
			Timestamp:  s.cfg.Now().UTC().Format(time.RFC3339Nano),
			Actor:      actor,
			Kind:       "fetch",
			Zone:       req.Zone,
			Domain:     domain,
			Status:     http.StatusOK,
			Cached:     true,
			RenderedJS: req.RenderJS,
			LatencyMS:  s.cfg.Now().Sub(started).Milliseconds(),
		})
		writeJSON(w, http.StatusOK, cloned)
		return
	}

	if req.Zone == ZoneExternal && !s.circuits.Allow(domain) {
		s.metrics.CircuitOpenTotal.Add(1)
		s.audit.Record(AuditEntry{
			Timestamp: s.cfg.Now().UTC().Format(time.RFC3339Nano),
			Actor:     actor,
			Kind:      "fetch",
			Zone:      req.Zone,
			Domain:    domain,
			Status:    http.StatusServiceUnavailable,
			Blocked:   true,
			LatencyMS: s.cfg.Now().Sub(started).Milliseconds(),
			Reason:    "circuit_open",
		})
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error":  fmt.Sprintf("domain circuit breaker open for %q", domain),
			"domain": domain,
		})
		return
	}

	if req.RenderJS && req.Zone == ZoneExternal && s.cfg.Backend.HeadlessConfigured() {
		renderedHTML, used, rerr := s.cfg.Backend.RenderJS(r.Context(), req.URL)
		if rerr == nil && used {
			s.circuits.RecordSuccess(domain)
			title, text := ExtractReadable(renderedHTML)
			resp := map[string]any{
				"url":          req.URL,
				"zone":         req.Zone,
				"status":       http.StatusOK,
				"title":        title,
				"content_type": "text/html; charset=utf-8",
				"text":         text,
				"truncated":    false,
				"cached":       false,
				"rendered_js":  true,
			}
			s.fetchCache.Set(cacheKey, cloneMap(resp))
			s.audit.Record(AuditEntry{
				Timestamp:  s.cfg.Now().UTC().Format(time.RFC3339Nano),
				Actor:      actor,
				Kind:       "fetch",
				Zone:       req.Zone,
				Domain:     domain,
				Status:     http.StatusOK,
				RenderedJS: true,
				LatencyMS:  s.cfg.Now().Sub(started).Milliseconds(),
			})
			writeJSON(w, http.StatusOK, resp)
			return
		}
	}

	finalURL, status, hdr, rawBody, truncated, redirects, err := s.cfg.Policy.FetchSafe(r.Context(), req.URL, req.Zone, req.MaxBytes, follow, s.cfg.Timeout)
	if err != nil {
		if errors.Is(err, ErrBlocked) {
			s.metrics.BlockedRequests.Add(1)
			s.audit.Record(AuditEntry{
				Timestamp: s.cfg.Now().UTC().Format(time.RFC3339Nano),
				Actor:     actor,
				Kind:      "fetch",
				Zone:      req.Zone,
				Domain:    domain,
				Status:    http.StatusForbidden,
				Blocked:   true,
				LatencyMS: s.cfg.Now().Sub(started).Milliseconds(),
				Reason:    "redirect_policy_blocked",
			})
			writeJSON(w, http.StatusForbidden, map[string]string{"error": err.Error()})
			return
		}
		if errors.Is(err, ErrInvalid) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		s.metrics.UpstreamErrors.Add(1)
		if req.Zone == ZoneExternal {
			s.circuits.RecordFailure(domain)
		}
		s.audit.Record(AuditEntry{
			Timestamp: s.cfg.Now().UTC().Format(time.RFC3339Nano),
			Actor:     actor,
			Kind:      "fetch",
			Zone:      req.Zone,
			Domain:    domain,
			Status:    http.StatusBadGateway,
			LatencyMS: s.cfg.Now().Sub(started).Milliseconds(),
			Reason:    "upstream_fetch_error",
		})
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": fmt.Sprintf("fetch failed: %v", err)})
		return
	}

	if req.Zone == ZoneExternal {
		if status >= http.StatusInternalServerError {
			s.metrics.UpstreamErrors.Add(1)
			s.circuits.RecordFailure(domain)
		} else {
			s.circuits.RecordSuccess(domain)
		}
	}

	contentType := ""
	if hdr != nil {
		contentType = hdr.Get("Content-Type")
	}
	text := string(rawBody)
	title := ""
	if readable && (strings.Contains(strings.ToLower(contentType), "html") || strings.HasPrefix(strings.TrimSpace(text), "<")) {
		title, text = ExtractReadable(text)
	}
	resp := map[string]any{
		"url":          finalURL,
		"zone":         req.Zone,
		"status":       status,
		"title":        title,
		"content_type": contentType,
		"text":         text,
		"truncated":    truncated,
		"cached":       false,
		"rendered_js":  false,
	}
	if len(redirects) > 0 {
		resp["redirects"] = redirects
	}
	if hdr != nil {
		if loc := hdr.Get("Location"); loc != "" {
			resp["location"] = loc
		}
	}
	s.fetchCache.Set(cacheKey, cloneMap(resp))
	s.audit.Record(AuditEntry{
		Timestamp: s.cfg.Now().UTC().Format(time.RFC3339Nano),
		Actor:     actor,
		Kind:      "fetch",
		Zone:      req.Zone,
		Domain:    domain,
		Status:    status,
		LatencyMS: s.cfg.Now().Sub(started).Milliseconds(),
	})
	writeJSON(w, http.StatusOK, resp)
}

func cloneMap(in map[string]any) map[string]any {
	out := make(map[string]any, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
