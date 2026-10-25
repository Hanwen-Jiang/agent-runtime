package server

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const (
	stateCookie  = "cw_oauth_state"
	tenantPrefix = "gh-"
)

// githubLogin 是 GitHub 的登录名：字母数字与单个连字符，不以连字符开头，最长 39。
var githubLogin = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9]|-[A-Za-z0-9]){0,38}$`)

// GitHub 是 OAuth App 与 REST API 的客户端。没配 Client ID 时为 nil，GitHub 登录与推送都不可用。
type GitHub struct {
	ClientID     string
	ClientSecret string
	// Web 是 https://github.com（或 GitHub Enterprise 的地址）；API 是 https://api.github.com。
	Web    string
	API    string
	Scopes string
	client *http.Client
}

func newGitHub(cfg Config) *GitHub {
	if cfg.GitHubClientID == "" || cfg.GitHubClientSecret == "" {
		return nil
	}
	gh := &GitHub{
		ClientID: cfg.GitHubClientID, ClientSecret: cfg.GitHubClientSecret,
		Web: strings.TrimRight(cfg.GitHubURL, "/"), API: strings.TrimRight(cfg.GitHubAPI, "/"), Scopes: cfg.GitHubScopes,
		client: &http.Client{Timeout: 30 * time.Second},
	}
	if gh.Web == "" {
		gh.Web = "https://github.com"
	}
	if gh.API == "" {
		gh.API = "https://api.github.com"
	}
	if gh.Scopes == "" {
		gh.Scopes = "repo read:user"
	}
	return gh
}

// GitHubError 是 GitHub 返回的错误；Message 不含令牌。
type GitHubError struct {
	Status  int
	Message string
}

func (e *GitHubError) Error() string { return fmt.Sprintf("GitHub %d：%s", e.Status, e.Message) }

func (g *GitHub) api(ctx context.Context, token, method, path string, body, out any) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, g.API+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	res, err := g.client.Do(req)
	if err != nil {
		return &GitHubError{Status: http.StatusBadGateway, Message: "连不上 GitHub"}
	}
	defer res.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(res.Body, 8<<20))
	if res.StatusCode >= 300 {
		var e struct {
			Message string `json:"message"`
			Errors  []struct {
				Message string `json:"message"`
			} `json:"errors"`
		}
		_ = json.Unmarshal(data, &e)
		msg := e.Message
		for _, sub := range e.Errors {
			if sub.Message != "" {
				msg += "：" + sub.Message
			}
		}
		return &GitHubError{Status: res.StatusCode, Message: msg}
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(data, out)
}

type githubUser struct {
	Login     string `json:"login"`
	ID        int64  `json:"id"`
	Name      string `json:"name"`
	AvatarURL string `json:"avatar_url"`
}

// exchange 用授权码换访问令牌。
func (g *GitHub) exchange(ctx context.Context, code, redirect string) (string, string, error) {
	form := url.Values{"client_id": {g.ClientID}, "client_secret": {g.ClientSecret}, "code": {code}, "redirect_uri": {redirect}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, g.Web+"/login/oauth/access_token", strings.NewReader(form.Encode()))
	if err != nil {
		return "", "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	res, err := g.client.Do(req)
	if err != nil {
		return "", "", errors.New("连不上 GitHub")
	}
	defer res.Body.Close()
	var out struct {
		AccessToken      string `json:"access_token"`
		Scope            string `json:"scope"`
		Error            string `json:"error"`
		ErrorDescription string `json:"error_description"`
	}
	if err := json.NewDecoder(io.LimitReader(res.Body, 1<<20)).Decode(&out); err != nil {
		return "", "", errors.New("GitHub 返回的令牌格式不对")
	}
	if out.AccessToken == "" {
		return "", "", fmt.Errorf("GitHub 拒绝了授权：%s %s", out.Error, out.ErrorDescription)
	}
	return out.AccessToken, out.Scope, nil
}

// --- 加密保存的令牌 ---

// GitHubAccount 是一个用户关联的 GitHub 账号；Token 加密存放。
type GitHubAccount struct {
	Login     string    `json:"login"`
	ID        int64     `json:"id"`
	Name      string    `json:"name,omitempty"`
	AvatarURL string    `json:"avatar_url,omitempty"`
	Scopes    string    `json:"scopes"`
	Token     string    `json:"token"`
	UpdatedAt time.Time `json:"updated_at"`
}

func (s *Server) accountFile(tenant, user string) string {
	return filepath.Join(s.cfg.DataDir, tenant, "users", user+".json")
}

// tokenKey 由开发签名密钥派生（MVP）；生产应换成 SecretStore 签发的密钥。
func (s *Server) tokenKey() []byte {
	mac := hmac.New(sha256.New, s.cfg.SigningKey)
	mac.Write([]byte("coding-web/github-token/v1"))
	return mac.Sum(nil)
}

func (s *Server) seal(plain, aad string) (string, error) {
	block, err := aes.NewCipher(s.tokenKey())
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(gcm.Seal(nonce, nonce, []byte(plain), []byte(aad))), nil
}

func (s *Server) open(sealed, aad string) (string, error) {
	data, err := base64.StdEncoding.DecodeString(sealed)
	if err != nil {
		return "", err
	}
	block, err := aes.NewCipher(s.tokenKey())
	if err != nil {
		return "", err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	if len(data) < gcm.NonceSize() {
		return "", errors.New("令牌记录损坏")
	}
	plain, err := gcm.Open(nil, data[:gcm.NonceSize()], data[gcm.NonceSize():], []byte(aad))
	if err != nil {
		return "", errors.New("令牌记录解不开")
	}
	return string(plain), nil
}

func (s *Server) saveAccount(tenant, user string, a GitHubAccount, token string) error {
	sealed, err := s.seal(token, tenant+"/"+user)
	if err != nil {
		return err
	}
	a.Token, a.UpdatedAt = sealed, time.Now().UTC()
	s.store.mu.Lock()
	defer s.store.mu.Unlock()
	return writeFileAtomic(s.accountFile(tenant, user), a)
}

// account 取出用户关联的 GitHub 账号与明文令牌；没有关联时 ok 为 false。
func (s *Server) account(id identity) (GitHubAccount, string, bool) {
	s.store.mu.Lock()
	a, err := readJSONFile[GitHubAccount](s.accountFile(id.TenantID, id.UserID))
	s.store.mu.Unlock()
	if err != nil || a.Token == "" {
		return GitHubAccount{}, "", false
	}
	token, err := s.open(a.Token, id.TenantID+"/"+id.UserID)
	if err != nil {
		return GitHubAccount{}, "", false
	}
	return a, token, true
}

// --- OAuth ---

func (s *Server) callbackURL() string {
	return strings.TrimRight(s.cfg.PublicURL, "/") + "/api/auth/github/callback"
}

func (s *Server) handleGitHubLogin(w http.ResponseWriter, r *http.Request) {
	if s.gh == nil {
		writeError(w, http.StatusNotFound, "没有配置 GitHub 登录")
		return
	}
	state := randomHex(24)
	http.SetCookie(w, &http.Cookie{Name: stateCookie, Value: state, Path: "/api/auth/github", HttpOnly: true, Secure: s.cfg.SecureCookie, SameSite: http.SameSiteLaxMode, MaxAge: 600})
	q := url.Values{"client_id": {s.gh.ClientID}, "redirect_uri": {s.callbackURL()}, "scope": {s.gh.Scopes}, "state": {state}, "allow_signup": {"false"}}
	http.Redirect(w, r, s.gh.Web+"/login/oauth/authorize?"+q.Encode(), http.StatusFound)
}

func (s *Server) handleGitHubCallback(w http.ResponseWriter, r *http.Request) {
	fail := func(msg string) { http.Redirect(w, r, "/#/login?error="+url.QueryEscape(msg), http.StatusFound) }
	if s.gh == nil {
		fail("没有配置 GitHub 登录")
		return
	}
	cookie, err := r.Cookie(stateCookie)
	http.SetCookie(w, &http.Cookie{Name: stateCookie, Value: "", Path: "/api/auth/github", HttpOnly: true, Secure: s.cfg.SecureCookie, SameSite: http.SameSiteLaxMode, MaxAge: -1})
	state := r.URL.Query().Get("state")
	if err != nil || state == "" || subtle.ConstantTimeCompare([]byte(cookie.Value), []byte(state)) != 1 {
		fail("登录状态不匹配，请重新登录")
		return
	}
	if e := r.URL.Query().Get("error"); e != "" {
		fail("GitHub 没有授权：" + e)
		return
	}
	ctx, cancel := ctxTimeout(r, 30*time.Second)
	defer cancel()
	token, scopes, err := s.gh.exchange(ctx, r.URL.Query().Get("code"), s.callbackURL())
	if err != nil {
		fail(err.Error())
		return
	}
	var u githubUser
	if err := s.gh.api(ctx, token, http.MethodGet, "/user", nil, &u); err != nil {
		fail("读不到 GitHub 账号：" + err.Error())
		return
	}
	if !githubLogin.MatchString(u.Login) {
		fail("GitHub 登录名格式不认识")
		return
	}
	user := strings.ToLower(u.Login)
	tenant := tenantPrefix + user
	if err := s.ensureTenantPolicy(tenant); err != nil {
		fail("写租户策略失败")
		return
	}
	if err := s.saveAccount(tenant, user, GitHubAccount{Login: u.Login, ID: u.ID, Name: u.Name, AvatarURL: u.AvatarURL, Scopes: scopes}, token); err != nil {
		fail("保存 GitHub 账号失败")
		return
	}
	s.setSession(w, tenant, user)
	http.Redirect(w, r, "/", http.StatusFound)
}

// --- 仓库 ---

type githubRepo struct {
	FullName      string `json:"full_name"`
	CloneURL      string `json:"clone_url"`
	HTMLURL       string `json:"html_url"`
	Private       bool   `json:"private"`
	DefaultBranch string `json:"default_branch"`
	Description   string `json:"description"`
	UpdatedAt     string `json:"updated_at"`
	Permissions   struct {
		Push bool `json:"push"`
	} `json:"permissions"`
}

func (s *Server) handleGitHubRepos(w http.ResponseWriter, r *http.Request, id identity) {
	if s.gh == nil {
		writeError(w, http.StatusNotFound, "没有配置 GitHub 登录")
		return
	}
	_, token, ok := s.account(id)
	if !ok {
		writeError(w, http.StatusForbidden, "先用 GitHub 登录")
		return
	}
	var repos []githubRepo
	ctx, cancel := ctxTimeout(r, 30*time.Second)
	defer cancel()
	if err := s.gh.api(ctx, token, http.MethodGet, "/user/repos?sort=updated&per_page=100&affiliation=owner,collaborator,organization_member", nil, &repos); err != nil {
		writeGitHubError(w, err)
		return
	}
	if repos == nil {
		repos = []githubRepo{}
	}
	writeJSON(w, http.StatusOK, repos)
}

// githubRepoOf 认出 GitHub 上的仓库地址，返回 owner/repo。
func (g *GitHub) repoOf(raw string) (string, bool) {
	if g == nil {
		return "", false
	}
	u, err := url.Parse(raw)
	if err != nil {
		return "", false
	}
	web, _ := url.Parse(g.Web)
	if web == nil || !strings.EqualFold(u.Host, web.Host) || u.Scheme != web.Scheme {
		return "", false
	}
	parts := strings.Split(strings.Trim(strings.TrimSuffix(u.Path, ".git"), "/"), "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", false
	}
	return parts[0] + "/" + parts[1], true
}

func writeGitHubError(w http.ResponseWriter, err error) {
	var ghErr *GitHubError
	if errors.As(err, &ghErr) {
		status := ghErr.Status
		if status == http.StatusUnauthorized {
			writeError(w, http.StatusForbidden, "GitHub 授权失效，请重新用 GitHub 登录")
			return
		}
		if status < 400 || status > 599 {
			status = http.StatusBadGateway
		}
		writeError(w, status, "GitHub："+ghErr.Message)
		return
	}
	writeError(w, http.StatusBadGateway, err.Error())
}
