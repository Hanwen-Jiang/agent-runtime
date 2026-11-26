package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/agentruntime/agent-runtime/services/go-crawler-gateway/internal/crawler"
)

func splitCSV(raw string) []string {
	var out []string
	for _, item := range strings.Split(raw, ",") {
		if clean := strings.TrimSpace(item); clean != "" {
			out = append(out, clean)
		}
	}
	return out
}

func envInt(name string, def int) int {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return def
	}
	if v, err := strconv.Atoi(raw); err == nil && v > 0 {
		return v
	}
	return def
}

func main() {
	addr := strings.TrimSpace(os.Getenv("AR_CRAWLER_ADDR"))
	if addr == "" {
		addr = ":8096"
	}
	timeout := time.Duration(envInt("AR_CRAWLER_TIMEOUT_S", 15)) * time.Second
	cacheTTL := time.Duration(envInt("AR_CRAWLER_CACHE_TTL_S", 300)) * time.Second

	policy := crawler.NewPolicy(crawler.PolicyConfig{
		ExternalAllowDomains: splitCSV(os.Getenv("AR_CRAWLER_EXTERNAL_ALLOW_DOMAINS")),
		ExternalDenyDomains:  splitCSV(os.Getenv("AR_CRAWLER_EXTERNAL_DENY_DOMAINS")),
		InternalAllowHosts:   splitCSV(os.Getenv("AR_CRAWLER_INTERNAL_ALLOW_HOSTS")),
	})
	searchUpstream := strings.TrimSpace(os.Getenv("AR_CRAWLER_SEARCH_UPSTREAM_URL"))
	if searchUpstream == "" {
		searchUpstream = strings.TrimSpace(os.Getenv("AR_CRAWLER_SEARCH_URL"))
	}
	backend := crawler.NewSearchBackend(crawler.SearchConfig{
		Provider:    strings.TrimSpace(os.Getenv("AR_CRAWLER_SEARCH_PROVIDER")),
		UpstreamURL: searchUpstream,
		APIKey:      strings.TrimSpace(os.Getenv("AR_CRAWLER_SEARCH_API_KEY")),
		ProviderKeys: map[string]string{
			"tavily": strings.TrimSpace(os.Getenv("AR_CRAWLER_TAVILY_KEY")),
			"brave":  strings.TrimSpace(os.Getenv("AR_CRAWLER_BRAVE_KEY")),
			"serper": strings.TrimSpace(os.Getenv("AR_CRAWLER_SERPER_KEY")),
			"exa":    strings.TrimSpace(os.Getenv("AR_CRAWLER_EXA_KEY")),
			"bocha":  strings.TrimSpace(os.Getenv("AR_CRAWLER_BOCHA_KEY")),
		},
		HeadlessURL: strings.TrimSpace(os.Getenv("AR_CRAWLER_HEADLESS_URL")),
		HeadlessTok: strings.TrimSpace(os.Getenv("AR_CRAWLER_HEADLESS_TOKEN")),
		Timeout:     timeout,
	})
	cbCooldown := time.Duration(envInt("AR_CRAWLER_CB_COOLDOWN_S", 120)) * time.Second
	srv := crawler.NewServer(crawler.ServerConfig{
		Token:       strings.TrimSpace(os.Getenv("AR_CRAWLER_TOKEN")),
		AdminToken:  strings.TrimSpace(os.Getenv("AR_CRAWLER_ADMIN_TOKEN")),
		RPM:         envInt("AR_CRAWLER_RPM", 60),
		CacheTTL:    cacheTTL,
		CacheSize:   envInt("AR_CRAWLER_CACHE_SIZE", 512),
		AuditSize:   envInt("AR_CRAWLER_AUDIT_SIZE", 500),
		CBThreshold: envInt("AR_CRAWLER_CB_THRESHOLD", 5),
		CBCooldown:  cbCooldown,
		Timeout:     timeout,
		Policy:      policy,
		Backend:     backend,
	})

	httpSrv := &http.Server{
		Addr:              addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Printf("crawler-gateway listening on %s (search=%s, internal_allow=%d)", addr, backend.ProviderName(), policy.InternalAllowCount())
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("crawler-gateway failed: %v", err)
		}
	}()

	<-ctx.Done()
	shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(shutCtx)
}
