package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// 登录方式（docs/plan/coding-agent-mvp.md 第 9 节）：GitHub、Gitee、GitLab（含自建）、Gitea/Forgejo/Codeberg、Bitbucket Cloud
// 这几家 Git 平台，以及只用来认人的 Google。都走 OAuth2 授权码流程；Git 平台的令牌还用来列仓库、克隆私有仓库、推送与开 PR/MR。
// 一个账号可以关联多家（accounts.go），用其中任何一家登录都是同一个账号。

// ProviderConfig 是一个登录方式的配置；Client ID 与 Secret 都给了才打开。
type ProviderConfig struct {
	ClientID     string
	ClientSecret string
	// URL 是网页地址（OAuth 授权与克隆地址的主机），自建的 GitLab、Gitea 填它的地址；API 不填时按 URL 推出。
	URL    string
	APIURL string
	Scopes string
	// Name 是显示名；Gitea 指向 codeberg.org 时默认叫 Codeberg。
	Name string
}

// providerSpec 是各家固定的部分。
type providerSpec struct {
	id, name string
	// prefix 是用这一家第一次登录时新账号的租户前缀（<prefix>-<登录名>）；开发登录不许用这些前缀。
	prefix string
	git    bool
	web    string
	// api 里的 {web} 换成网页地址（自建实例跟着变）。
	api string
	// authorize、token 是相对网页地址的路径，或绝对地址（Google）。
	authorize, token string
	scopes           string
	// basicClient：换令牌时 client_id/secret 放 Basic 头（Bitbucket）。
	basicClient bool
	// queryToken：API 的令牌放 access_token 查询参数（Gitee）。
	queryToken bool
	extra      url.Values
}

var providerSpecs = []providerSpec{
	{id: "github", name: "GitHub", prefix: "gh", git: true, web: "https://github.com", api: "https://api.github.com",
		authorize: "/login/oauth/authorize", token: "/login/oauth/access_token", scopes: "repo read:user", extra: url.Values{"allow_signup": {"false"}}},
	{id: "gitee", name: "Gitee", prefix: "gitee", git: true, web: "https://gitee.com", api: "{web}/api/v5",
		authorize: "/oauth/authorize", token: "/oauth/token", scopes: "user_info projects pull_requests", queryToken: true},
	{id: "gitlab", name: "GitLab", prefix: "gl", git: true, web: "https://gitlab.com", api: "{web}/api/v4",
		authorize: "/oauth/authorize", token: "/oauth/token", scopes: "api read_user"},
	{id: "gitea", name: "Gitea", prefix: "gitea", git: true, web: "https://codeberg.org", api: "{web}/api/v1",
		authorize: "/login/oauth/authorize", token: "/login/oauth/access_token"},
	{id: "bitbucket", name: "Bitbucket", prefix: "bb", git: true, web: "https://bitbucket.org", api: "https://api.bitbucket.org/2.0",
		authorize: "/site/oauth2/authorize", token: "/site/oauth2/access_token", basicClient: true},
	{id: "google", name: "Google", prefix: "google", web: "https://accounts.google.com", api: "https://openidconnect.googleapis.com/v1",
		authorize: "https://accounts.google.com/o/oauth2/v2/auth", token: "https://oauth2.googleapis.com/token", scopes: "openid email profile",
		extra: url.Values{"prompt": {"select_account"}}},
}

// Provider 是一个打开了的登录方式。
type Provider struct {
	providerSpec
	Name                   string
	ClientID, ClientSecret string
	Scopes                 string
	Web, API               string
	AuthorizeURL, TokenURL string
	client                 *http.Client
}

func specOf(id string) (providerSpec, bool) {
	for _, sp := range providerSpecs {
		if sp.id == id {
			return sp, true
		}
	}
	return providerSpec{}, false
}

// newProviders 按配置打开登录方式；GitHub 也可以用旧的 GitHub* 字段配置。
func newProviders(cfg Config) map[string]*Provider {
	confs := map[string]ProviderConfig{}
	for id, c := range cfg.Providers {
		confs[id] = c
	}
	if _, ok := confs["github"]; !ok && cfg.GitHubClientID != "" {
		confs["github"] = ProviderConfig{ClientID: cfg.GitHubClientID, ClientSecret: cfg.GitHubClientSecret, URL: cfg.GitHubURL, APIURL: cfg.GitHubAPI, Scopes: cfg.GitHubScopes}
	}
	out := map[string]*Provider{}
	for _, sp := range providerSpecs {
		c, ok := confs[sp.id]
		if !ok || c.ClientID == "" || c.ClientSecret == "" {
			continue
		}
		p := &Provider{providerSpec: sp, Name: sp.name, ClientID: c.ClientID, ClientSecret: c.ClientSecret, Scopes: sp.scopes, Web: sp.web, client: &http.Client{Timeout: 30 * time.Second}}
		if c.URL != "" && sp.id != "google" {
			p.Web = strings.TrimRight(c.URL, "/")
		}
		p.API = strings.ReplaceAll(sp.api, "{web}", p.Web)
		if c.APIURL != "" {
			p.API = strings.TrimRight(c.APIURL, "/")
		}
		if c.Scopes != "" {
			p.Scopes = c.Scopes
		}
		p.AuthorizeURL, p.TokenURL = sp.authorize, sp.token
		if !strings.HasPrefix(sp.authorize, "https://") {
			p.AuthorizeURL, p.TokenURL = p.Web+sp.authorize, p.Web+sp.token
		}
		switch {
		case c.Name != "":
			p.Name = c.Name
		case sp.id == "gitea" && strings.Contains(p.Web, "codeberg.org"):
			p.Name = "Codeberg"
		}
		out[sp.id] = p
	}
	return out
}

func (p *Provider) authorizeURL(redirect, state string) string {
	q := url.Values{"client_id": {p.ClientID}, "redirect_uri": {redirect}, "response_type": {"code"}, "state": {state}}
	if p.Scopes != "" {
		q.Set("scope", p.Scopes)
	}
	for k, v := range p.extra {
		q[k] = v
	}
	return p.AuthorizeURL + "?" + q.Encode()
}

// tokenSet 是换到的令牌；没有过期时间的（GitHub OAuth App）ExpiresAt 为零。
type tokenSet struct {
	Access, Refresh, Scope string
	ExpiresAt              time.Time
}

func (p *Provider) exchange(ctx context.Context, code, redirect string) (tokenSet, error) {
	return p.tokenRequest(ctx, url.Values{"grant_type": {"authorization_code"}, "code": {code}, "redirect_uri": {redirect}})
}

func (p *Provider) refresh(ctx context.Context, refresh string) (tokenSet, error) {
	return p.tokenRequest(ctx, url.Values{"grant_type": {"refresh_token"}, "refresh_token": {refresh}})
}

func (p *Provider) tokenRequest(ctx context.Context, form url.Values) (tokenSet, error) {
	if !p.basicClient {
		form.Set("client_id", p.ClientID)
		form.Set("client_secret", p.ClientSecret)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.TokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return tokenSet{}, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", userAgent) // Gitee 没有 User-Agent 时返回 403
	if p.basicClient {
		req.SetBasicAuth(p.ClientID, p.ClientSecret)
	}
	res, err := p.client.Do(req)
	if err != nil {
		return tokenSet{}, fmt.Errorf("连不上 %s", p.Name)
	}
	defer res.Body.Close()
	var out struct {
		AccessToken      string          `json:"access_token"`
		RefreshToken     string          `json:"refresh_token"`
		Scope            string          `json:"scope"`
		Scopes           string          `json:"scopes"`
		ExpiresIn        json.Number     `json:"expires_in"`
		Error            json.RawMessage `json:"error"`
		ErrorDescription string          `json:"error_description"`
	}
	if err := json.NewDecoder(io.LimitReader(res.Body, 1<<20)).Decode(&out); err != nil {
		return tokenSet{}, fmt.Errorf("%s 返回的令牌格式不对", p.Name)
	}
	if out.AccessToken == "" {
		return tokenSet{}, fmt.Errorf("%s 拒绝了授权：%s %s", p.Name, strings.Trim(string(out.Error), `"`), out.ErrorDescription)
	}
	t := tokenSet{Access: out.AccessToken, Refresh: out.RefreshToken, Scope: out.Scope}
	if t.Scope == "" {
		t.Scope = out.Scopes
	}
	if secs, err := out.ExpiresIn.Int64(); err == nil && secs > 0 {
		t.ExpiresAt = time.Now().UTC().Add(time.Duration(secs) * time.Second)
	}
	return t, nil
}

const userAgent = "coding-web (agent-runtime)"

// GitError 是 Git 平台（或 Google）返回的错误；Message 不含令牌。
type GitError struct {
	Provider string
	Status   int
	Message  string
}

func (e *GitError) Error() string { return fmt.Sprintf("%s %d：%s", e.Provider, e.Status, e.Message) }

// api 调平台的 REST API。path 是相对 API 地址的路径（可带查询串）。令牌放 Authorization 头；Gitee 放 access_token 查询参数。
// 连接失败时的错误不带地址（地址里可能有令牌）。
func (p *Provider) api(ctx context.Context, token, method, path string, body, out any) error {
	target := p.API + path
	if p.queryToken {
		sep := "?"
		if strings.Contains(target, "?") {
			sep = "&"
		}
		target += sep + "access_token=" + url.QueryEscape(token)
	}
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, target, reader)
	if err != nil {
		return &GitError{Provider: p.Name, Status: http.StatusBadGateway, Message: "请求地址不对"}
	}
	req.Header.Set("User-Agent", userAgent)
	if p.id == "github" {
		req.Header.Set("Accept", "application/vnd.github+json")
		req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	} else {
		req.Header.Set("Accept", "application/json")
	}
	if !p.queryToken {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	res, err := p.client.Do(req)
	if err != nil {
		return &GitError{Provider: p.Name, Status: http.StatusBadGateway, Message: "连不上 " + p.Name}
	}
	defer res.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(res.Body, 8<<20))
	if res.StatusCode >= 300 {
		return &GitError{Provider: p.Name, Status: res.StatusCode, Message: errorMessage(data)}
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(data, out)
}

// errorMessage 从各家的错误正文里取出说明：GitHub、Gitee、Gitea 的 message（GitHub 另有 errors），GitLab 的 message 可能是数组或对象，
// Bitbucket 的 error.message，OAuth 的 error_description。
func errorMessage(data []byte) string {
	var e map[string]json.RawMessage
	if json.Unmarshal(data, &e) != nil {
		return strings.TrimSpace(string(data[:min(len(data), 200)]))
	}
	var parts []string
	for _, key := range []string{"message", "error_description", "error"} {
		raw, ok := e[key]
		if !ok {
			continue
		}
		var s string
		if json.Unmarshal(raw, &s) == nil {
			parts = append(parts, s)
			continue
		}
		var nested struct {
			Message string `json:"message"`
		}
		if json.Unmarshal(raw, &nested) == nil && nested.Message != "" {
			parts = append(parts, nested.Message)
			continue
		}
		parts = append(parts, string(raw))
	}
	if raw, ok := e["errors"]; ok {
		var subs []struct {
			Message string `json:"message"`
		}
		if json.Unmarshal(raw, &subs) == nil {
			for _, sub := range subs {
				if sub.Message != "" {
					parts = append(parts, sub.Message)
				}
			}
		}
	}
	return strings.Join(parts, "：")
}

func writeGitError(w http.ResponseWriter, err error) {
	var ge *GitError
	if errors.As(err, &ge) {
		status := ge.Status
		if status == http.StatusUnauthorized {
			writeError(w, http.StatusForbidden, ge.Provider+" 授权失效，请在“账号与平台”里重新连接 "+ge.Provider)
			return
		}
		if status < 400 || status > 599 {
			status = http.StatusBadGateway
		}
		writeError(w, status, ge.Provider+"："+ge.Message)
		return
	}
	writeError(w, http.StatusBadGateway, err.Error())
}
