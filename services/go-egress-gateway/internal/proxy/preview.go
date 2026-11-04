package proxy

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/netip"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
)

// 预览入口（进网，设计书改动稿 W8）：https://<环境号>.<预览域名>/…，经集群的入口转发到这里。
//
//   - 令牌是环境凭据（用途 preview，q.port 是环境的端口），第一次放在查询参数 ar_preview 里；校验通过后写进同名的
//     Cookie（HttpOnly、Secure、SameSite=Lax，到期同令牌）并重定向到去掉参数的地址，之后的请求带 Cookie。
//   - 令牌的环境号要与主机名里的一致；授权（sandbox_grants 里这个环境这个端口的预览授权）有效、环境还在，才转发到授权记的地址。
//     授权收回、到期，或环境没了，令牌就跟着失效。
//   - 每个环境每秒的请求有上限（PreviewRPS）。转发时去掉令牌的 Cookie。WebSocket 照常升级。
//   - 每个请求一条审计（direction = preview）。

var envLabel = regexp.MustCompile(`^sbe-[0-9a-f]{16}$`)

const previewParam = "ar_preview"

// Preview 是预览入口的 Handler。
func (g *Gateway) Preview() http.Handler {
	return http.HandlerFunc(g.servePreview)
}

func (g *Gateway) servePreview(w http.ResponseWriter, r *http.Request) {
	started := g.cfg.Now()
	host := strings.ToLower(r.Host)
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	suffix := "." + strings.ToLower(strings.Trim(g.cfg.PreviewDomain, "."))
	env := strings.TrimSuffix(host, suffix)
	if g.cfg.PreviewDomain == "" || env == host || !envLabel.MatchString(env) {
		http.NotFound(w, r)
		return
	}
	token, fromQuery := r.URL.Query().Get(previewParam), true
	if token == "" {
		fromQuery = false
		if c, err := r.Cookie(previewParam); err == nil {
			token = c.Value
		}
	}
	claims, err := envcred.Verify(g.cfg.Key, token, envcred.PurposePreview, g.cfg.Now())
	if err != nil || claims.EnvRef != env {
		g.denied.Add(1)
		g.count("invalid_preview_token")
		http.Error(w, "预览地址要有效的令牌", http.StatusUnauthorized)
		return
	}
	port := int(claims.Limits["port"])
	rec := g.record(claims, "preview", host, port, started)
	if !g.allowPreview(env) {
		g.refuse(w, rec, nil, &refusal{http.StatusTooManyRequests, "rate_limited"})
		return
	}
	grant, found, err := g.cfg.Store.PreviewGrant(r.Context(), claims.TenantID, env, port)
	if err != nil {
		g.refuse(w, rec, nil, &refusal{http.StatusServiceUnavailable, "store_unavailable"})
		return
	}
	if !found {
		g.refuse(w, rec, nil, &refusal{http.StatusForbidden, "preview_not_granted"})
		return
	}
	rec.GrantID, rec.TaskID = grant.ID, grant.TaskID
	target, err := netip.ParseAddrPort(grant.PreviewTarget)
	if err != nil {
		g.refuse(w, rec, nil, &refusal{http.StatusBadGateway, "preview_target_invalid"})
		return
	}
	rec.Address = target.String()
	if fromQuery {
		// 入口（Traefik、Cloudflare）终结 TLS 后转到这里：按 X-Forwarded-Proto 判断用户那一侧是不是 HTTPS。
		secure := r.TLS != nil || strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https")
		http.SetCookie(w, &http.Cookie{
			Name: previewParam, Value: token, Path: "/", HttpOnly: true, Secure: secure, SameSite: http.SameSiteLaxMode,
			Expires: time.Unix(claims.ExpiresAt, 0),
		})
		clean := *r.URL
		q := clean.Query()
		q.Del(previewParam)
		clean.RawQuery = q.Encode()
		clean.Scheme, clean.Host = "", ""
		g.allowed.Add(1)
		rec.Result = "allowed"
		g.finish(rec)
		http.Redirect(w, r, clean.String(), http.StatusFound)
		return
	}
	g.allowed.Add(1)
	counter := &countingWriter{ResponseWriter: w}
	var up int64
	if r.Body != nil && r.Body != http.NoBody {
		r.Body = &meteredBody{src: r.Body, add: func(n int64) string { up += n; return "" }, reason: new(string)}
	}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(&url.URL{Scheme: "http", Host: target.String()})
			pr.Out.Host = pr.In.Host
			stripPreviewCookie(pr.Out)
		},
		ModifyResponse: func(resp *http.Response) error {
			hostOnlyCookies(resp.Header)
			return nil
		},
		Transport: &http.Transport{
			Proxy: nil,
			// 进网直连沙箱（集群里 Pod 的地址）：不经出网用的 Dialer。
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				d := net.Dialer{Timeout: 10 * time.Second}
				return d.DialContext(ctx, "tcp", target.String())
			},
			ResponseHeaderTimeout: time.Minute,
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			rec.Reason = "upstream_failed"
			http.Error(w, "预览：沙箱里的服务没有响应", http.StatusBadGateway)
		},
	}
	proxy.ServeHTTP(counter, r)
	rec.UpBytes, rec.DownBytes, rec.Result = up, counter.n, "allowed"
	if rec.Reason != "" {
		rec.Result = "closed"
	}
	g.finish(rec)
}

// hostOnlyCookies 让沙箱发的 Cookie 只留在环境自己的主机上：预览地址与平台的站点同在一个注册域下，带 Domain 的 Cookie
// 会被浏览器带去别的站。去掉 Domain 属性，丢掉冒充网关令牌（ar_preview）的；别的属性原样。
func hostOnlyCookies(h http.Header) {
	lines := h.Values("Set-Cookie")
	if len(lines) == 0 {
		return
	}
	h.Del("Set-Cookie")
	for _, line := range lines {
		parts := strings.Split(line, ";")
		name, _, _ := strings.Cut(strings.TrimSpace(parts[0]), "=")
		if strings.TrimSpace(name) == previewParam {
			continue
		}
		kept := parts[:1]
		for _, attr := range parts[1:] {
			key, _, _ := strings.Cut(strings.TrimSpace(attr), "=")
			if !strings.EqualFold(strings.TrimSpace(key), "domain") {
				kept = append(kept, attr)
			}
		}
		h.Add("Set-Cookie", strings.Join(kept, ";"))
	}
}

func stripPreviewCookie(r *http.Request) {
	cookies := r.Cookies()
	r.Header.Del("Cookie")
	for _, c := range cookies {
		if c.Name != previewParam {
			r.AddCookie(c)
		}
	}
}

type countingWriter struct {
	http.ResponseWriter
	n int64
}

func (c *countingWriter) Write(p []byte) (int, error) {
	n, err := c.ResponseWriter.Write(p)
	c.n += int64(n)
	return n, err
}

func (c *countingWriter) Flush() {
	if f, ok := c.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (c *countingWriter) Unwrap() http.ResponseWriter { return c.ResponseWriter }

// Admin 是管理端口：/healthz、/metrics（不要令牌，网络策略只放行 Runtime 与 observability），
// POST /admin/revoke {"tenant_id","task_id"}（要 Bearer 管理令牌；没配令牌时拒绝）：收回这个 Task 的授权并立即断开它的连接。
func (g *Gateway) Admin(token string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") })
	mux.HandleFunc("GET /metrics", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		g.Metrics(w)
	})
	mux.HandleFunc("POST /admin/revoke", func(w http.ResponseWriter, r *http.Request) {
		got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if token == "" || subtle.ConstantTimeCompare([]byte(got), []byte(token)) != 1 {
			http.Error(w, "要管理令牌", http.StatusUnauthorized)
			return
		}
		var body struct {
			TenantID string `json:"tenant_id"`
			TaskID   string `json:"task_id"`
		}
		if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&body); err != nil || body.TenantID == "" || body.TaskID == "" ||
			!validID.MatchString(body.TenantID) || len(body.TaskID) > 128 {
			http.Error(w, "要 tenant_id 与 task_id", http.StatusBadRequest)
			return
		}
		n, err := g.cfg.Store.Revoke(r.Context(), body.TenantID, body.TaskID)
		if err != nil {
			http.Error(w, "收回失败", http.StatusServiceUnavailable)
			return
		}
		closed := g.CloseTask(body.TenantID, body.TaskID, "grant_revoked")
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]int64{"revoked": n, "closed": int64(closed)})
	})
	return mux
}

var validID = regexp.MustCompile(`^(_platform|[a-z0-9][a-z0-9_-]{0,62})$`)
