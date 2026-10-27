package proxy

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
)

// previewClient 把 <环境>.preview.example.test 的请求都送到测试里的预览入口。
func (h *harness) previewClient() *http.Client {
	jar, _ := cookiejar.New(nil)
	addr := h.preview.Listener.Addr().String()
	return &http.Client{
		Jar:     jar,
		Timeout: 5 * time.Second,
		Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "tcp", addr)
		}},
	}
}

func (h *harness) previewToken(env string, port int64, ttl time.Duration) string {
	tok, err := envcred.Issue(h.key, envcred.Claims{EnvRef: env, TenantID: tenant, ExpiresAt: h.clock.Now().Add(ttl).Unix(), Purpose: "preview", Limits: map[string]int64{"port": port}})
	if err != nil {
		h.t.Fatal(err)
	}
	return tok
}

func previewURL(env, path string) string {
	return fmt.Sprintf("http://%s.preview.example.test%s", env, path)
}

// 验收 6：预览地址不带令牌或令牌过期访问被拒；授权收回后失效。
func TestPreviewNeedsValidTokenAndLiveGrant(t *testing.T) {
	h := newHarness(t)
	app := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := r.Cookie("ar_preview"); err == nil {
			http.Error(w, "令牌的 Cookie 不该转给沙箱里的服务", http.StatusBadRequest)
			return
		}
		fmt.Fprintf(w, "callback %s host=%s", r.URL.Query().Get("code"), r.Host)
	}))
	defer app.Close()
	h.grant(grantSpec{id: "sbg-00000000000000aa", hosts: []string{"api.example.com"}, previewEnv: envRef, previewPort: 3000, previewTarget: app.Listener.Addr().String()})
	c := h.previewClient()

	get := func(rawURL string) (int, string) {
		resp, err := c.Get(rawURL)
		if err != nil {
			t.Fatalf("GET %s：%v", rawURL, err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, string(b)
	}
	if status, _ := get(previewURL(envRef, "/oauth/callback?code=abc")); status != http.StatusUnauthorized {
		t.Fatalf("没有令牌：%d", status)
	}
	expired := h.previewToken(envRef, 3000, -time.Minute)
	if status, _ := get(previewURL(envRef, "/cb?ar_preview="+url.QueryEscape(expired))); status != http.StatusUnauthorized {
		t.Fatalf("过期的令牌：%d", status)
	}
	other := h.previewToken("sbe-ffffffffffffffff", 3000, time.Hour)
	if status, _ := get(previewURL(envRef, "/cb?ar_preview="+url.QueryEscape(other))); status != http.StatusUnauthorized {
		t.Fatalf("别的环境的令牌：%d", status)
	}
	wrongPort := h.previewToken(envRef, 4000, time.Hour)
	if status, _ := get(previewURL(envRef, "/cb?ar_preview="+url.QueryEscape(wrongPort))); status != http.StatusForbidden {
		t.Fatalf("没有授权的端口：%d", status)
	}

	// 第一次带令牌：写 Cookie 并重定向到去掉令牌的地址；之后靠 Cookie。
	good := h.previewToken(envRef, 3000, time.Hour)
	status, body := get(previewURL(envRef, "/oauth/callback?code=abc&ar_preview="+url.QueryEscape(good)))
	if status != 200 || !strings.HasPrefix(body, "callback abc host="+envRef+".preview.example.test") {
		t.Fatalf("有效的令牌：%d %s", status, body)
	}
	if status, body := get(previewURL(envRef, "/again?code=x")); status != 200 || !strings.HasPrefix(body, "callback x") {
		t.Fatalf("凭 Cookie：%d %s", status, body)
	}

	// 授权收回之后同一个令牌失效。
	h.exec("UPDATE sandbox_grants SET state = 'revoked' WHERE grant_id = 'sbg-00000000000000aa'")
	if status, _ := get(previewURL(envRef, "/again")); status != http.StatusForbidden {
		t.Fatalf("收回之后：%d", status)
	}
	waitFor(t, "预览的审计", func() bool {
		for _, r := range h.audits() {
			if r["direction"] == "preview" && r["result"] == "allowed" {
				return true
			}
		}
		return false
	})
}

func TestPreviewRateLimitAndUnknownHosts(t *testing.T) {
	h := newHarness(t)
	app := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") }))
	defer app.Close()
	h.grant(grantSpec{id: "sbg-00000000000000ab", hosts: []string{"api.example.com"}, previewEnv: envRef, previewPort: 3000, previewTarget: app.Listener.Addr().String()})
	c := h.previewClient()
	tok := url.QueryEscape(h.previewToken(envRef, 3000, time.Hour))
	codes := map[int]int{}
	for range 8 {
		resp, err := c.Get(previewURL(envRef, "/x?ar_preview="+tok))
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		codes[resp.StatusCode]++
	}
	if codes[http.StatusTooManyRequests] == 0 {
		t.Fatalf("每秒 8 个的上限没起作用：%v", codes)
	}
	resp, err := c.Get("http://not-an-env.preview.example.test/")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("不是环境号的主机：%d", resp.StatusCode)
	}
}
