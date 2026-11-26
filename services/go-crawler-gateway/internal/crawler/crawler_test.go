package crawler

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestPolicyExternalSSRFAndInternalAllowlist(t *testing.T) {
	ctx := context.Background()
	pol := NewPolicy(PolicyConfig{
		ExternalDenyDomains: []string{"evil.example"},
		InternalAllowHosts:  []string{"ar-agent-runtime-task-service:8080", "coding-web:8095"},
		LookupIP: func(_ context.Context, _ string, host string) ([]net.IP, error) {
			switch host {
			case "rebind.test":
				return []net.IP{net.ParseIP("127.0.0.1")}, nil
			case "public.example":
				return []net.IP{net.ParseIP("93.184.216.34")}, nil
			default:
				return nil, net.UnknownNetworkError("unknown")
			}
		},
	})

	blocked := []string{
		"http://127.0.0.1:8080/healthz",
		"http://10.0.0.5/secret",
		"http://169.254.169.254/latest/meta-data/",
		"http://[::1]:8080/",
		"http://[::ffff:127.0.0.1]/",
		"http://rebind.test/admin",
		"https://sub.evil.example/payload",
		"https://user:pass@public.example/",
		"file:///etc/passwd",
	}
	for _, raw := range blocked {
		if _, _, _, err := pol.ValidateTarget(ctx, raw, ZoneExternal); err == nil {
			t.Fatalf("expected external URL blocked: %s", raw)
		}
	}

	if _, port, ips, err := pol.ValidateTarget(ctx, "https://public.example/docs", ZoneExternal); err != nil || port != 443 || len(ips) != 1 {
		t.Fatalf("expected public.example allowed: port=%d ips=%v err=%v", port, ips, err)
	}

	// Internal zone: only explicit allowlist passes; infra/DB ports and metadata always denied.
	if _, port, _, err := pol.ValidateTarget(ctx, "http://ar-agent-runtime-task-service:8080/healthz", ZoneInternal); err != nil || port != 8080 {
		t.Fatalf("expected allowlisted internal service permitted, got err=%v", err)
	}
	for _, badInt := range []string{
		"http://ar-agent-runtime-task-service:9999/",
		"http://pg-rw.data.svc:5432/",
		"http://redpanda.data.svc:9093/",
		"http://169.254.169.254/latest/meta-data/",
		"https://kubernetes.default.svc/api",
	} {
		if _, _, _, err := pol.ValidateTarget(ctx, badInt, ZoneInternal); err == nil {
			t.Fatalf("expected non-allowlisted/sensitive internal target blocked: %s", badInt)
		}
	}
}

func TestExtractReadableAndParseDDG(t *testing.T) {
	raw := `<html><head><title>Guide &amp; Spec</title><style>.x{}</style><script>alert(1)</script></head><body><nav>Home</nav><main><h1>Intro</h1><p>Clean &lt;content&gt; line.</p></main><footer>Copyright</footer></body></html>`
	title, text := ExtractReadable(raw)
	if title != "Guide & Spec" {
		t.Fatalf("unexpected title: %q", title)
	}
	if strings.Contains(text, "alert(1)") || strings.Contains(text, "Home") || strings.Contains(text, "Copyright") {
		t.Fatalf("boilerplate leaked into extracted text: %q", text)
	}
	if !strings.Contains(text, "Clean <content> line.") {
		t.Fatalf("missing clean content in %q", text)
	}

	ddg := `<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgo.dev%2Fdoc">Go Docs</a><a class="result__snippet">Official Go documentation</a>`
	rows := ParseDDGHTML(ddg)
	if len(rows) != 1 || rows[0].URL != "https://go.dev/doc" || rows[0].Domain != "go.dev" {
		t.Fatalf("unexpected DDG parse result: %+v", rows)
	}
}

func TestServerSearchFetchCacheAuthAndRateLimit(t *testing.T) {
	targetSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/doc":
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Header().Set("Set-Cookie", "session=secret")
			_, _ = w.Write([]byte("<html><title>DocPage</title><body><p>Hello Crawler</p></body></html>"))
		case "/redir-ssrf":
			http.Redirect(w, r, "http://127.0.0.1:8080/secret", http.StatusFound)
		default:
			http.NotFound(w, r)
		}
	}))
	defer targetSrv.Close()

	ddgSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`<a class="result__a" href="https://docs.python.org/3/">Python Docs</a><a class="result__snippet">Python stdlib</a>`))
	}))
	defer ddgSrv.Close()

	u, _ := url.Parse(targetSrv.URL)
	pol := NewPolicy(PolicyConfig{
		InternalAllowHosts:  []string{u.Host},
		AllowPrivateForTest: true,
	})
	backend := NewSearchBackend(SearchConfig{
		Provider:    "ddg_lite",
		UpstreamURL: ddgSrv.URL,
		Timeout:     5 * time.Second,
	})
	srv := NewServer(ServerConfig{
		Token:   "crawler-test-secret",
		RPM:     10,
		Policy:  pol,
		Backend: backend,
	})
	api := httptest.NewServer(srv.Handler())
	defer api.Close()

	// Unauthorized
	res, err := http.Post(api.URL+"/v1/fetch", "application/json", strings.NewReader(`{"url":"https://example.com"}`))
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", res.StatusCode)
	}

	call := func(method, path string, body any) (int, map[string]any) {
		var rdr *bytes.Reader
		if body != nil {
			b, _ := json.Marshal(body)
			rdr = bytes.NewReader(b)
		} else {
			rdr = bytes.NewReader(nil)
		}
		req, _ := http.NewRequest(method, api.URL+path, rdr)
		req.Header.Set("Authorization", "Bearer crawler-test-secret")
		req.Header.Set("Content-Type", "application/json")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&out)
		return resp.StatusCode, out
	}

	// Internal allowlisted fetch + cache hit
	code, f1 := call(http.MethodPost, "/v1/fetch", map[string]any{"url": targetSrv.URL + "/doc", "zone": "internal"})
	if code != 200 || f1["cached"] != false || !strings.Contains(f1["text"].(string), "Hello Crawler") {
		t.Fatalf("first fetch failed (%d): %#v", code, f1)
	}
	code, f2 := call(http.MethodPost, "/v1/fetch", map[string]any{"url": targetSrv.URL + "/doc", "zone": "internal"})
	if code != 200 || f2["cached"] != true {
		t.Fatalf("expected cached fetch hit, got (%d): %#v", code, f2)
	}

	// Search + cache hit
	code, s1 := call(http.MethodPost, "/v1/search", map[string]any{"query": "python docs", "include_domains": []string{"docs.python.org"}})
	if code != 200 || s1["provider"] != "ddg_lite" || s1["total"] != float64(1) {
		t.Fatalf("search failed (%d): %#v", code, s1)
	}

	// External SSRF blocked even when AllowPrivateForTest is false on a clean server
	cleanSrv := httptest.NewServer(NewServer(ServerConfig{Token: "crawler-test-secret"}).Handler())
	defer cleanSrv.Close()
	reqSSRF, _ := http.NewRequest(http.MethodPost, cleanSrv.URL+"/v1/fetch", strings.NewReader(`{"url":"http://127.0.0.1:8080/healthz","zone":"external"}`))
	reqSSRF.Header.Set("Authorization", "Bearer crawler-test-secret")
	resSSRF, err := http.DefaultClient.Do(reqSSRF)
	if err != nil {
		t.Fatal(err)
	}
	resSSRF.Body.Close()
	if resSSRF.StatusCode != http.StatusForbidden {
		t.Fatalf("expected external SSRF 403, got %d", resSSRF.StatusCode)
	}

	// Status + Prometheus metrics
	code, statusObj := call(http.MethodGet, "/v1/status", nil)
	if code != 200 || statusObj["ok"] != true {
		t.Fatalf("status failed (%d): %#v", code, statusObj)
	}
	mResp, err := http.Get(api.URL + "/metrics")
	if err != nil {
		t.Fatal(err)
	}
	mRaw, _ := ioReadAllString(mResp)
	mResp.Body.Close()
	if !strings.Contains(mRaw, "crawler_up 1") || !strings.Contains(mRaw, "crawler_cache_hits_total 1") {
		t.Fatalf("unexpected /metrics output:\n%s", mRaw)
	}
}

func ioReadAllString(r *http.Response) (string, error) {
	buf := new(bytes.Buffer)
	_, err := buf.ReadFrom(r.Body)
	return buf.String(), err
}

func TestCircuitBreakerAuditAndAllowlistGovernance(t *testing.T) {
	now := time.Date(2026, 11, 26, 12, 0, 0, 0, time.UTC)
	var failUpstream bool
	var reqCount int
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reqCount++
		if failUpstream {
			http.Error(w, "upstream down", http.StatusBadGateway)
			return
		}
		_, _ = w.Write([]byte("<html><body><p>Recovered</p></body></html>"))
	}))
	defer upstream.Close()

	pol := NewPolicy(PolicyConfig{
		InternalAllowHosts:  []string{"ar-agent-runtime-task-service:8080"},
		AllowPrivateForTest: true,
	})
	srv := NewServer(ServerConfig{
		Token:       "worker-token",
		AdminToken:  "admin-gov-token",
		RPM:         50,
		CBThreshold: 2,
		CBCooldown:  30 * time.Second,
		AuditSize:   20,
		Policy:      pol,
		Now:         func() time.Time { return now },
	})
	api := httptest.NewServer(srv.Handler())
	defer api.Close()

	doReq := func(tok, method, path string, body any) (int, map[string]any) {
		var rdr *bytes.Reader
		if body != nil {
			b, _ := json.Marshal(body)
			rdr = bytes.NewReader(b)
		} else {
			rdr = bytes.NewReader(nil)
		}
		req, _ := http.NewRequest(method, api.URL+path, rdr)
		req.Header.Set("Authorization", "Bearer "+tok)
		req.Header.Set("Content-Type", "application/json")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&out)
		return resp.StatusCode, out
	}

	// 1. Trigger circuit breaker open after 2 external upstream 502 responses
	failUpstream = true
	for i := 0; i < 2; i++ {
		code, _ := doReq("worker-token", http.MethodPost, "/v1/fetch", map[string]any{
			"url":  upstream.URL + "/flaky/" + string(rune('a'+i)),
			"zone": ZoneExternal,
		})
		if code != 200 {
			t.Fatalf("expected 200 wrapper for 502 upstream response, got %d", code)
		}
	}
	// 3rd call must fast-fail 503 without hitting upstream
	before := reqCount
	code, cbErr := doReq("worker-token", http.MethodPost, "/v1/fetch", map[string]any{
		"url":  upstream.URL + "/flaky/c",
		"zone": ZoneExternal,
	})
	if code != http.StatusServiceUnavailable || reqCount != before {
		t.Fatalf("expected 503 circuit open without hitting upstream: code=%d reqDelta=%d body=%#v", code, reqCount-before, cbErr)
	}

	// Advance past cooldown -> recovers
	now = now.Add(31 * time.Second)
	failUpstream = false
	code, rec := doReq("worker-token", http.MethodPost, "/v1/fetch", map[string]any{
		"url":  upstream.URL + "/flaky/c",
		"zone": ZoneExternal,
	})
	if code != 200 || !strings.Contains(rec["text"].(string), "Recovered") {
		t.Fatalf("expected recovery after cooldown, got (%d): %#v", code, rec)
	}

	// 2. Audit log captures requests and supports blocked filtering
	code, auditAll := doReq("worker-token", http.MethodGet, "/v1/audit?limit=10", nil)
	if code != 200 || auditAll["count"].(float64) < 4 {
		t.Fatalf("expected audit entries, got (%d): %#v", code, auditAll)
	}
	code, auditBlocked := doReq("worker-token", http.MethodGet, "/v1/audit?blocked=true", nil)
	if code != 200 || auditBlocked["count"].(float64) < 1 {
		t.Fatalf("expected blocked audit entry from open circuit, got (%d): %#v", code, auditBlocked)
	}

	// 3. Internal allowlist governance: worker token cannot mutate (403); infra ports rejected (403); admin can propose + approve
	code, _ = doReq("worker-token", http.MethodPost, "/v1/policy/internal-allowlist/propose", map[string]any{
		"action": "add", "target": "coding-web:8095",
	})
	if code != http.StatusForbidden {
		t.Fatalf("expected worker token forbidden on propose, got %d", code)
	}
	code, _ = doReq("admin-gov-token", http.MethodPost, "/v1/policy/internal-allowlist/propose", map[string]any{
		"action": "add", "target": "pg-rw.data.svc:5432", "reason": "bad",
	})
	if code != http.StatusForbidden {
		t.Fatalf("expected DB port blocked in proposal, got %d", code)
	}
	code, prop := doReq("admin-gov-token", http.MethodPost, "/v1/policy/internal-allowlist/propose", map[string]any{
		"action": "add", "target": "coding-web:8095", "reason": "enable admin proxy",
	})
	if code != http.StatusCreated {
		t.Fatalf("expected 201 proposal, got %d: %#v", code, prop)
	}
	code, appResp := doReq("admin-gov-token", http.MethodPost, "/v1/policy/internal-allowlist/approve", map[string]any{
		"id": prop["id"],
	})
	if code != 200 {
		t.Fatalf("expected 200 approve, got %d: %#v", code, appResp)
	}
	code, listResp := doReq("worker-token", http.MethodGet, "/v1/policy/internal-allowlist", nil)
	if code != 200 || len(listResp["allowlist"].([]any)) != 2 {
		t.Fatalf("expected updated allowlist with 2 entries, got (%d): %#v", code, listResp)
	}
}
