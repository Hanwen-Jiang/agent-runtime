package server

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
)

const fakeToken = "gho_test_token_123"

// fakeGitHub 是 GitHub 的 OAuth 与 REST API 的替身：记下收到的 PR 请求与令牌。
type fakeGitHub struct {
	mu     sync.Mutex
	pulls  []map[string]any
	auths  []string
	server *httptest.Server
}

func newFakeGitHub(t *testing.T) *fakeGitHub {
	f := &fakeGitHub{}
	f.server = httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.server.Close)
	return f
}

func (f *fakeGitHub) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	if a := r.Header.Get("Authorization"); a != "" {
		f.auths = append(f.auths, a)
	}
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/login/oauth/access_token":
		_ = r.ParseForm()
		if r.Form.Get("code") != "good-code" || r.Form.Get("client_secret") != "secret" {
			_ = json.NewEncoder(w).Encode(map[string]string{"error": "bad_verification_code"})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"access_token": fakeToken, "scope": "repo,read:user", "token_type": "bearer"})
	case r.URL.Path == "/user":
		_ = json.NewEncoder(w).Encode(map[string]any{"login": "Octo-Cat", "id": 42, "name": "Octo", "avatar_url": "https://avatars.githubusercontent.com/u/42"})
	case r.URL.Path == "/user/repos":
		_ = json.NewEncoder(w).Encode([]map[string]any{{"full_name": "octo-cat/demo", "clone_url": f.server.URL + "/octo-cat/demo.git", "private": true, "default_branch": "main", "permissions": map[string]bool{"push": true}}})
	case r.Method == http.MethodPost && r.URL.Path == "/repos/octo-cat/demo/pulls":
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if len(f.pulls) > 0 {
			w.WriteHeader(http.StatusUnprocessableEntity)
			_ = json.NewEncoder(w).Encode(map[string]any{"message": "Validation Failed", "errors": []map[string]string{{"message": "A pull request already exists for octo-cat:" + body["head"].(string) + "."}}})
			return
		}
		f.pulls = append(f.pulls, body)
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"html_url": "https://github.com/octo-cat/demo/pull/7", "number": 7, "draft": true})
	case r.Method == http.MethodGet && r.URL.Path == "/repos/octo-cat/demo/pulls":
		_ = json.NewEncoder(w).Encode([]map[string]any{{"html_url": "https://github.com/octo-cat/demo/pull/7", "number": 7}})
	default:
		w.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(w).Encode(map[string]string{"message": "Not Found"})
	}
}

// fakeForge 是其余几家（GitLab、Gitee、Gitea、Bitbucket、Google）的替身：OAuth 的换令牌与续期、账号、仓库列表、开 PR/MR。
// 换令牌时检查 client 认证（Bitbucket 用 Basic，其余放表单）；Gitee 的 API 只认 access_token 查询参数。
type fakeForge struct {
	kind          string
	mu            sync.Mutex
	server        *httptest.Server
	user          map[string]any
	expiresIn     int
	rejectDraft   bool
	tokens        map[string]bool
	refreshTokens map[string]bool
	issued        int
	refreshes     int
	pulls         []map[string]any
	pullPaths     []string
	userAgents    []string
}

func newFakeForge(t *testing.T, kind string, user map[string]any) *fakeForge {
	f := &fakeForge{kind: kind, user: user, tokens: map[string]bool{}, refreshTokens: map[string]bool{}}
	f.server = httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.server.Close)
	return f
}

func (f *fakeForge) fail(w http.ResponseWriter, status int, msg string) {
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"message": msg})
}

func (f *fakeForge) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	path := r.URL.Path
	if r.Method == http.MethodPost && (strings.HasSuffix(path, "/token") || strings.HasSuffix(path, "/access_token")) {
		f.userAgents = append(f.userAgents, r.UserAgent())
		_ = r.ParseForm()
		if f.kind == "bitbucket" {
			if id, secret, ok := r.BasicAuth(); !ok || id != "client" || secret != "secret" {
				f.fail(w, http.StatusUnauthorized, "client credentials must be in the Basic header")
				return
			}
		} else if r.Form.Get("client_id") != "client" || r.Form.Get("client_secret") != "secret" {
			f.fail(w, http.StatusUnauthorized, "bad client")
			return
		}
		switch r.Form.Get("grant_type") {
		case "authorization_code":
			if r.Form.Get("code") != "good-code" {
				w.WriteHeader(http.StatusBadRequest)
				_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid_grant"})
				return
			}
		case "refresh_token":
			if !f.refreshTokens[r.Form.Get("refresh_token")] {
				w.WriteHeader(http.StatusBadRequest)
				_ = json.NewEncoder(w).Encode(map[string]string{"error": "invalid_grant"})
				return
			}
			f.refreshes++
		}
		f.issued++
		tok, ref := fmt.Sprintf("%s-tok-%d", f.kind, f.issued), fmt.Sprintf("%s-ref-%d", f.kind, f.issued)
		f.tokens[tok], f.refreshTokens[ref] = true, true
		out := map[string]any{"access_token": tok, "refresh_token": ref, "token_type": "bearer", "scope": "api"}
		if f.expiresIn > 0 {
			out["expires_in"] = f.expiresIn
		}
		_ = json.NewEncoder(w).Encode(out)
		return
	}
	tok := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if f.kind == "gitee" {
		tok = r.URL.Query().Get("access_token")
	}
	if !f.tokens[tok] {
		f.fail(w, http.StatusUnauthorized, "401 Unauthorized")
		return
	}
	switch {
	case strings.HasSuffix(path, "/user") || strings.HasSuffix(path, "/userinfo"):
		_ = json.NewEncoder(w).Encode(f.user)
	case r.Method == http.MethodGet && (strings.HasSuffix(path, "/user/repos") || strings.HasSuffix(path, "/projects") || strings.HasSuffix(path, "/repositories")):
		_ = json.NewEncoder(w).Encode(f.repos())
	case r.Method == http.MethodPost && (strings.HasSuffix(path, "/pulls") || strings.HasSuffix(path, "/merge_requests") || strings.HasSuffix(path, "/pullrequests")):
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.pulls = append(f.pulls, body)
		f.pullPaths = append(f.pullPaths, r.URL.EscapedPath())
		if f.rejectDraft && body["draft"] != nil {
			w.WriteHeader(http.StatusBadRequest)
			_ = json.NewEncoder(w).Encode(map[string]any{"type": "error", "error": map[string]string{"message": "Unknown field: draft"}})
			return
		}
		w.WriteHeader(http.StatusCreated)
		switch f.kind {
		case "gitlab":
			_ = json.NewEncoder(w).Encode(map[string]any{"web_url": f.server.URL + "/group/sub/demo/-/merge_requests/5", "iid": 5})
		case "bitbucket":
			_ = json.NewEncoder(w).Encode(map[string]any{"id": 9, "links": map[string]any{"html": map[string]string{"href": "https://bitbucket.org/team/demo/pull-requests/9"}}})
		default:
			_ = json.NewEncoder(w).Encode(map[string]any{"html_url": f.server.URL + "/pulls/7", "number": 7})
		}
	default:
		f.fail(w, http.StatusNotFound, "Not Found")
	}
}

func (f *fakeForge) repos() any {
	switch f.kind {
	case "gitlab":
		return []map[string]any{{"path_with_namespace": "group/sub/demo", "http_url_to_repo": f.server.URL + "/group/sub/demo.git", "visibility": "private", "default_branch": "main"}}
	case "gitee":
		return []map[string]any{{"full_name": "alice/demo", "html_url": f.server.URL + "/alice/demo", "private": true, "default_branch": "master", "permission": map[string]bool{"push": true}}}
	case "bitbucket":
		return map[string]any{"values": []map[string]any{{"full_name": "team/demo", "is_private": true, "mainbranch": map[string]string{"name": "main"},
			"links": map[string]any{"clone": []map[string]string{{"name": "https", "href": "https://bob@bitbucket.org/team/demo.git"}, {"name": "ssh", "href": "git@bitbucket.org:team/demo.git"}}}}}}
	default:
		return []map[string]any{{"full_name": "alice/demo", "clone_url": f.server.URL + "/alice/demo.git", "private": false, "default_branch": "main", "permissions": map[string]bool{"push": true}}}
	}
}

// pointProvider 让一家登录方式指向替身服务（网页、API、授权与换令牌的地址照各家的路径）。
func pointProvider(srv *Server, id, base string) {
	p := srv.providers[id]
	p.Web = base
	switch id {
	case "github":
		p.API, p.AuthorizeURL, p.TokenURL = base, base+"/login/oauth/authorize", base+"/login/oauth/access_token"
	case "gitlab":
		p.API, p.AuthorizeURL, p.TokenURL = base+"/api/v4", base+"/oauth/authorize", base+"/oauth/token"
	case "gitee":
		p.API, p.AuthorizeURL, p.TokenURL = base+"/api/v5", base+"/oauth/authorize", base+"/oauth/token"
	case "gitea":
		p.API, p.AuthorizeURL, p.TokenURL = base+"/api/v1", base+"/login/oauth/authorize", base+"/login/oauth/access_token"
	case "bitbucket":
		p.API, p.AuthorizeURL, p.TokenURL = base+"/2.0", base+"/site/oauth2/authorize", base+"/site/oauth2/access_token"
	case "google":
		p.API, p.AuthorizeURL, p.TokenURL = base+"/v1", base+"/auth", base+"/token"
	}
}

// newOAuthServer 起 coding-web，打开给定的几家（client/secret），回调地址指向测试服务。
func newOAuthServer(t *testing.T, ids ...string) (*httptest.Server, Config, *Server) {
	t.Helper()
	ts, _, cfg, srv := newServerWith(t, func(c *Config) {
		c.Providers = map[string]ProviderConfig{}
		for _, id := range ids {
			c.Providers[id] = ProviderConfig{ClientID: "client", ClientSecret: "secret"}
		}
		c.PublicURL = "http://placeholder"
	})
	srv.cfg.PublicURL = ts.URL
	srv.git.AllowFileProtocol = true
	return ts, cfg, srv
}

// oauthFlow 走一遍授权：从登录入口拿到 state，带着授权码回调；返回回调的响应（302）。
func oauthFlow(t *testing.T, c *client, ts *httptest.Server, provider, query, code string) *http.Response {
	t.Helper()
	c.http.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	res, err := c.http.Get(ts.URL + "/api/auth/" + provider + "/login" + query)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != http.StatusFound {
		t.Fatalf("%s 登录入口应 302，实际 %d", provider, res.StatusCode)
	}
	loc, _ := url.Parse(res.Header.Get("Location"))
	if loc.Query().Get("redirect_uri") != ts.URL+"/api/auth/"+provider+"/callback" || loc.Query().Get("response_type") != "code" {
		t.Fatalf("%s 授权地址不对：%s", provider, loc)
	}
	res, err = c.http.Get(ts.URL + "/api/auth/" + provider + "/callback?code=" + code + "&state=" + loc.Query().Get("state"))
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	return res
}

func githubLoginFlow(t *testing.T, ts *httptest.Server, code string) (*client, *http.Response) {
	c := newClient(t, ts.URL)
	return c, oauthFlow(t, c, ts, "github", "", code)
}

func gitIn(t *testing.T, s *Server, dir string, args ...string) string {
	t.Helper()
	out, err := s.git.RunAs(t.Context(), dir, "t", "t@example.com", args...)
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(out)
}

// publishable 建一个示例工作区，把它记成某家平台上的仓库，开一个会话，会话的 origin 指向本地裸仓库（当作平台上的仓库）。
func publishable(t *testing.T, c *client, cfg Config, srv *Server, tenant, provider, repo string) (api, ws, bare string) {
	t.Helper()
	_, project, raw := c.do("POST", "/api/projects", map[string]string{"name": "demo", "sample": "ci_rounding"})
	pid, _ := project["id"].(string)
	if pid == "" {
		t.Fatalf("建工作区：%s", raw)
	}
	base := filepath.Join(cfg.WorkspaceRoot, tenant, pid)
	bare = filepath.Join(t.TempDir(), "demo.git")
	gitIn(t, srv, filepath.Dir(bare), "init", "-q", "--bare", "-b", "main", bare)
	gitIn(t, srv, base, "remote", "add", "origin", bare)
	gitIn(t, srv, base, "push", "-q", "origin", "main")
	if _, err := srv.updateProject(tenant, pid, func(p *Project) { p.Provider, p.Repo, p.BaseBranch = provider, repo, "main" }); err != nil {
		t.Fatal(err)
	}
	api, ws = newSession(t, c, cfg, tenant, pid)
	gitIn(t, srv, ws, "remote", "set-url", "origin", bare)
	gitIn(t, srv, ws, "fetch", "-q", "origin")
	return api, ws, bare
}

func TestGitHubLoginPublishAndReset(t *testing.T) {
	gh := newFakeGitHub(t)
	ts, cfg, srv := newOAuthServer(t, "github")
	pointProvider(srv, "github", gh.server.URL)

	// 状态不匹配被拒。
	bad := newClient(t, ts.URL)
	bad.http.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	res, _ := bad.http.Get(ts.URL + "/api/auth/github/callback?code=good-code&state=forged")
	if res.StatusCode != http.StatusFound || !strings.Contains(res.Header.Get("Location"), "error=") {
		t.Fatalf("伪造的 state 应被拒：%d %s", res.StatusCode, res.Header.Get("Location"))
	}

	c, res := githubLoginFlow(t, ts, "good-code")
	if res.StatusCode != http.StatusFound || res.Header.Get("Location") != "/" {
		t.Fatalf("回调应登录并回到首页：%d %s", res.StatusCode, res.Header.Get("Location"))
	}
	_, me, _ := c.do("GET", "/api/me", nil)
	if me["tenant"] != "gh-octo-cat" || me["user"] != "octo-cat" || me["github"].(map[string]any)["login"] != "Octo-Cat" {
		t.Fatalf("登录身份不对：%v", me)
	}
	stored, _ := os.ReadFile(filepath.Join(cfg.DataDir, "gh-octo-cat", "users", "octo-cat.json"))
	if len(stored) == 0 || strings.Contains(string(stored), fakeToken) {
		t.Fatalf("令牌应加密存放：%s", stored)
	}
	if code, _, raw := c.do("GET", "/api/git/github/repos", nil); code != http.StatusOK || !strings.Contains(raw, "octo-cat/demo") || !strings.Contains(raw, `"can_push":true`) {
		t.Fatalf("仓库列表：%d %s", code, raw)
	}

	api, ws, bare := publishable(t, c, cfg, srv, "gh-octo-cat", "github", "octo-cat/demo")
	if err := os.WriteFile(filepath.Join(ws, "pricing.py"), []byte("# fixed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	code, out, raw := c.do("POST", api+"/publish", map[string]string{"title": "Fix rounding"})
	if code != http.StatusOK || out["created"] != true || out["pr_number"].(float64) != 7 {
		t.Fatalf("推送并开 PR：%d %s", code, raw)
	}
	branch := out["branch"].(string)
	if !regexp.MustCompile(`^agent/fix-rounding-[0-9a-f]{4}$`).MatchString(branch) {
		t.Fatalf("分支名不对：%s", branch)
	}
	if got := gitIn(t, srv, bare, "rev-parse", "--verify", "refs/heads/"+branch); got == "" {
		t.Fatal("远端应有 agent 分支")
	}
	if main := gitIn(t, srv, bare, "log", "-1", "--format=%s", "main"); main != "Initial commit" {
		t.Fatalf("不能动远端的 main：%s", main)
	}
	if len(gh.pulls) != 1 || gh.pulls[0]["head"] != branch || gh.pulls[0]["base"] != "main" || gh.pulls[0]["draft"] != true {
		t.Fatalf("PR 请求不对：%v", gh.pulls)
	}
	for _, a := range gh.auths {
		if a != "Bearer "+fakeToken {
			t.Fatalf("调 GitHub API 要带用户的令牌：%q", a)
		}
	}
	if author := gitIn(t, srv, ws, "log", "-1", "--format=%an <%ae>"); author != "Octo-Cat <42+Octo-Cat@users.noreply.github.com>" {
		t.Fatalf("提交作者应是 GitHub 账号：%s", author)
	}
	if cfgText, _ := os.ReadFile(filepath.Join(ws, ".git", "config")); strings.Contains(string(cfgText), fakeToken) || strings.Contains(string(cfgText), "extraheader") {
		t.Fatal("令牌不能写进 .git/config")
	}

	if code, _, _ := c.do("POST", api+"/publish", map[string]string{"title": "again"}); code != http.StatusBadRequest {
		t.Fatalf("没有新改动时应 400，实际 %d", code)
	}
	_ = os.WriteFile(filepath.Join(ws, "notes.md"), []byte("more\n"), 0o644)
	code, out, raw = c.do("POST", api+"/publish", map[string]string{"title": "More changes"})
	if code != http.StatusOK || out["created"] != false || out["branch"] != branch || len(gh.pulls) != 1 {
		t.Fatalf("再推应只更新同一个 PR：%d %s", code, raw)
	}
	if out["session"].(map[string]any)["pr_number"].(float64) != 7 {
		t.Fatalf("PR 记在会话上：%s", raw)
	}

	code, out, raw = c.do("POST", api+"/reset-branch", nil)
	if code != http.StatusOK || out["session"].(map[string]any)["branch"] != nil {
		t.Fatalf("回到基线分支：%d %s", code, raw)
	}
	if cur := srv.git.Branch(t.Context(), ws); cur != "main" {
		t.Fatalf("应回到 main，实际 %s", cur)
	}
}

func TestOneAccountLinksSeveralProviders(t *testing.T) {
	gh := newFakeGitHub(t)
	gl := newFakeForge(t, "gitlab", map[string]any{"id": 77, "username": "octo", "name": "Octo GL", "avatar_url": "https://gitlab.example/a.png"})
	ts, _, srv := newOAuthServer(t, "github", "gitlab")
	pointProvider(srv, "github", gh.server.URL)
	pointProvider(srv, "gitlab", gl.server.URL)

	c, _ := githubLoginFlow(t, ts, "good-code")
	res := oauthFlow(t, c, ts, "gitlab", "?link=1", "good-code")
	if res.StatusCode != http.StatusFound || res.Header.Get("Location") != "/#/?linked=gitlab" {
		t.Fatalf("连接 GitLab 应回到首页并提示：%d %s", res.StatusCode, res.Header.Get("Location"))
	}
	_, acct, raw := c.do("GET", "/api/account", nil)
	if conns, _ := acct["connections"].([]any); len(conns) != 2 || !strings.Contains(raw, `"login":"octo"`) || strings.Contains(raw, "gitlab-tok") {
		t.Fatalf("账号应连着 GitHub 与 GitLab（不含令牌）：%s", raw)
	}

	// 退出后用 GitLab 登录，还是同一个账号。
	again := newClient(t, ts.URL)
	if res := oauthFlow(t, again, ts, "gitlab", "", "good-code"); res.Header.Get("Location") != "/" {
		t.Fatalf("GitLab 登录：%s", res.Header.Get("Location"))
	}
	if _, me, _ := again.do("GET", "/api/me", nil); me["tenant"] != "gh-octo-cat" || me["user"] != "octo-cat" {
		t.Fatalf("用关联的另一家登录应是同一个账号：%v", me)
	}

	// 别的账号不能再连这个 GitLab 账号。
	other := newClient(t, ts.URL)
	other.do("POST", "/api/login", map[string]string{"tenant": "acme", "user": "bob"})
	res = oauthFlow(t, other, ts, "gitlab", "?link=1", "good-code")
	if loc, _ := url.QueryUnescape(res.Header.Get("Location")); !strings.Contains(loc, "已经关联到别的账号") {
		t.Fatalf("同一个 GitLab 账号不能关联两个本地账号：%s", loc)
	}

	// 断开：平台建的账号至少留一个登录方式。
	if code, _, raw := c.do("POST", "/api/account/connections/github/disconnect", nil); code != http.StatusOK || strings.Contains(raw, `"provider":"github"`) {
		t.Fatalf("断开 GitHub：%d %s", code, raw)
	}
	if code, _, _ := c.do("POST", "/api/account/connections/gitlab/disconnect", nil); code != http.StatusConflict {
		t.Fatalf("最后一个登录方式不能断开，实际 %d", code)
	}
}

func TestLinkingNeedsALogin(t *testing.T) {
	gl := newFakeForge(t, "gitlab", map[string]any{"id": 1, "username": "x"})
	ts, _, srv := newOAuthServer(t, "gitlab")
	pointProvider(srv, "gitlab", gl.server.URL)
	c := newClient(t, ts.URL)
	c.http.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	res, _ := c.http.Get(ts.URL + "/api/auth/gitlab/login?link=1")
	if loc := res.Header.Get("Location"); !strings.HasPrefix(loc, "/#/login?error=") {
		t.Fatalf("没登录时连接应回登录页：%s", loc)
	}
}

func TestGitLabTokenRefreshReposAndMergeRequest(t *testing.T) {
	gl := newFakeForge(t, "gitlab", map[string]any{"id": 77, "username": "alice", "name": "Alice", "public_email": "alice@example.com"})
	gl.expiresIn = 30 // 两分钟内就过期：下一次用之前先续期
	ts, cfg, srv := newOAuthServer(t, "gitlab")
	pointProvider(srv, "gitlab", gl.server.URL)
	c := newClient(t, ts.URL)
	c.do("POST", "/api/login", map[string]string{"tenant": "acme", "user": "alice"})
	oauthFlow(t, c, ts, "gitlab", "?link=1", "good-code")

	code, _, raw := c.do("GET", "/api/git/gitlab/repos", nil)
	var repos []remoteRepo
	_ = json.Unmarshal([]byte(raw), &repos)
	if code != http.StatusOK || len(repos) != 1 || repos[0].FullName != "group/sub/demo" || !repos[0].Private || !repos[0].CanPush || repos[0].Provider != "gitlab" {
		t.Fatalf("GitLab 仓库：%d %s", code, raw)
	}
	if gl.refreshes != 1 {
		t.Fatalf("快过期的令牌应先续期，续期 %d 次", gl.refreshes)
	}

	api, ws, _ := publishable(t, c, cfg, srv, "acme", "gitlab", "group/sub/demo")
	_ = os.WriteFile(filepath.Join(ws, "pricing.py"), []byte("# fixed\n"), 0o644)
	code, out, raw := c.do("POST", api+"/publish", map[string]string{"title": "Fix rounding"})
	if code != http.StatusOK || out["pr_number"].(float64) != 5 || !strings.Contains(out["pr_url"].(string), "merge_requests/5") {
		t.Fatalf("开 MR：%d %s", code, raw)
	}
	mr := gl.pulls[0]
	if gl.pullPaths[0] != "/api/v4/projects/group%2Fsub%2Fdemo/merge_requests" || mr["title"] != "Draft: Fix rounding" || mr["target_branch"] != "main" || !strings.HasPrefix(mr["source_branch"].(string), "agent/") {
		t.Fatalf("MR 请求不对：%s %v", gl.pullPaths[0], mr)
	}
	if author := gitIn(t, srv, ws, "log", "-1", "--format=%an <%ae>"); author != "alice <alice@example.com>" {
		t.Fatalf("提交作者应是 GitLab 账号：%s", author)
	}
}

func TestGiteeSendsTheTokenAsAQueryParameter(t *testing.T) {
	ge := newFakeForge(t, "gitee", map[string]any{"id": 9, "login": "Li-Lei", "name": "李雷", "avatar_url": "https://gitee.com/a.png"})
	ts, _, srv := newOAuthServer(t, "gitee")
	pointProvider(srv, "gitee", ge.server.URL)
	c := newClient(t, ts.URL)
	if res := oauthFlow(t, c, ts, "gitee", "", "good-code"); res.Header.Get("Location") != "/" {
		t.Fatalf("Gitee 登录：%s", res.Header.Get("Location"))
	}
	if _, me, _ := c.do("GET", "/api/me", nil); me["tenant"] != "gitee-li-lei" || me["name"] != "李雷" {
		t.Fatalf("第一次用 Gitee 登录应建 gitee-<登录名> 的账号：%v", me)
	}
	if !strings.HasPrefix(ge.userAgents[0], "coding-web") {
		t.Fatalf("换令牌要带 User-Agent（Gitee 没有时返回 403）：%q", ge.userAgents)
	}
	code, _, raw := c.do("GET", "/api/git/gitee/repos", nil)
	if code != http.StatusOK || !strings.Contains(raw, ge.server.URL+"/alice/demo.git") {
		t.Fatalf("Gitee 仓库（克隆地址由 html_url 拼出）：%d %s", code, raw)
	}
}

func TestBitbucketBasicClientAuthAndDraftFallback(t *testing.T) {
	bb := newFakeForge(t, "bitbucket", map[string]any{"uuid": "{b0b}", "username": "bob", "display_name": "Bob",
		"links": map[string]any{"avatar": map[string]string{"href": "https://bitbucket.org/b.png"}}})
	bb.rejectDraft = true
	ts, cfg, srv := newOAuthServer(t, "bitbucket")
	pointProvider(srv, "bitbucket", bb.server.URL)
	c := newClient(t, ts.URL)
	oauthFlow(t, c, ts, "bitbucket", "", "good-code")
	if _, me, _ := c.do("GET", "/api/me", nil); me["tenant"] != "bb-bob" {
		t.Fatalf("Bitbucket 登录（client 认证放 Basic 头）：%v", me)
	}
	_, _, raw := c.do("GET", "/api/git/bitbucket/repos", nil)
	if !strings.Contains(raw, `"clone_url":"https://bitbucket.org/team/demo.git"`) {
		t.Fatalf("克隆地址要去掉里面的用户名：%s", raw)
	}

	api, ws, _ := publishable(t, c, cfg, srv, "bb-bob", "bitbucket", "team/demo")
	_ = os.WriteFile(filepath.Join(ws, "pricing.py"), []byte("# fixed\n"), 0o644)
	code, out, raw := c.do("POST", api+"/publish", map[string]string{"title": "Fix"})
	if code != http.StatusOK || out["pr_number"].(float64) != 9 {
		t.Fatalf("开 PR：%d %s", code, raw)
	}
	if len(bb.pulls) != 2 || bb.pulls[0]["draft"] != true || bb.pulls[1]["draft"] != nil {
		t.Fatalf("不认 draft 时应退回普通 PR：%v", bb.pulls)
	}
	if bb.pullPaths[1] != "/2.0/repositories/team/demo/pullrequests" {
		t.Fatalf("PR 地址：%s", bb.pullPaths[1])
	}
}

func TestGoogleIsOnlyALoginMethod(t *testing.T) {
	g := newFakeForge(t, "google", map[string]any{"sub": "1234", "email": "zhang.san@example.com", "email_verified": true, "name": "张三", "picture": "https://lh3.googleusercontent.com/p"})
	ts, _, srv := newOAuthServer(t, "google")
	pointProvider(srv, "google", g.server.URL)
	c := newClient(t, ts.URL)
	oauthFlow(t, c, ts, "google", "", "good-code")
	if _, me, _ := c.do("GET", "/api/me", nil); me["tenant"] != "google-zhang-san" || me["avatar_url"] != "https://lh3.googleusercontent.com/p" {
		t.Fatalf("Google 登录：%v", me)
	}
	if code, _, _ := c.do("GET", "/api/git/google/repos", nil); code != http.StatusNotFound {
		t.Fatalf("Google 不是 Git 平台，实际 %d", code)
	}

	g.user["email_verified"] = false
	g.user["sub"] = "999"
	res := oauthFlow(t, newClient(t, ts.URL), ts, "google", "", "good-code")
	if loc, _ := url.QueryUnescape(res.Header.Get("Location")); !strings.Contains(loc, "邮箱还没有验证") {
		t.Fatalf("邮箱没验证的 Google 账号不能登录：%s", loc)
	}
}

func TestLegacyGitHubAccountsKeepTheirTenant(t *testing.T) {
	gh := newFakeGitHub(t)
	ts, cfg, srv := newOAuthServer(t, "github")
	pointProvider(srv, "github", gh.server.URL)
	// 多平台之前的账号文件：只有 GitHub，令牌按（租户、用户）加密；没有 identities.json。
	sealed, err := srv.seal("old-token", "gh-octo-cat/octo-cat")
	if err != nil {
		t.Fatal(err)
	}
	legacy := map[string]any{"login": "Octo-Cat", "id": 42, "scopes": "repo", "token": sealed}
	if err := writeFileAtomic(srv.accountFile("gh-octo-cat", "octo-cat"), legacy); err != nil {
		t.Fatal(err)
	}
	c, _ := githubLoginFlow(t, ts, "good-code")
	if _, me, _ := c.do("GET", "/api/me", nil); me["tenant"] != "gh-octo-cat" {
		t.Fatalf("旧账号应沿用原来的租户：%v", me)
	}
	stored, _ := os.ReadFile(srv.accountFile("gh-octo-cat", "octo-cat"))
	ids, _ := os.ReadFile(filepath.Join(cfg.DataDir, "identities.json"))
	if !strings.Contains(string(stored), `"connections"`) || strings.Contains(string(stored), `"token": "`+sealed) || !strings.Contains(string(ids), `"github:42"`) {
		t.Fatalf("旧账号应迁移成多平台格式并记下对应：%s %s", stored, ids)
	}
}

func TestDevLoginCannotTakeProviderTenants(t *testing.T) {
	ts, _, _ := newServer(t)
	c := newClient(t, ts.URL)
	for _, tenant := range []string{"gh-octo-cat", "gitee-li", "gl-x", "gitea-x", "bb-x", "google-x"} {
		if code, _, _ := c.do("POST", "/api/login", map[string]string{"tenant": tenant, "user": "u"}); code != http.StatusBadRequest {
			t.Fatalf("开发登录不能用 %s，实际 %d", tenant, code)
		}
	}
}

func TestPublishNeedsAGitProjectAndConnection(t *testing.T) {
	ts, cfg, _ := newOAuthServer(t, "gitlab")
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)
	api, _ := newSession(t, c, cfg, "acme", pid)
	if code, _, _ := c.do("POST", api+"/publish", map[string]string{"title": "x"}); code != http.StatusBadRequest {
		t.Fatalf("不是 Git 平台的工作区不能推送，实际 %d", code)
	}
}

func TestGitCredentialsAndRepoPaths(t *testing.T) {
	providers := newProviders(Config{Providers: map[string]ProviderConfig{
		"github": {ClientID: "c", ClientSecret: "s"}, "gitee": {ClientID: "c", ClientSecret: "s"}, "gitlab": {ClientID: "c", ClientSecret: "s", URL: "https://git.corp.example"},
		"gitea": {ClientID: "c", ClientSecret: "s"}, "bitbucket": {ClientID: "c", ClientSecret: "s"}, "google": {ClientID: "c", ClientSecret: "s"},
	}})
	conn := Connection{Login: "li-lei", RemoteID: "9"}
	want := map[string][2]string{
		"github": {"tok", "x-oauth-basic"}, "gitee": {"li-lei", "tok"}, "gitlab": {"oauth2", "tok"}, "gitea": {"oauth2", "tok"}, "bitbucket": {"x-token-auth", "tok"},
	}
	for id, w := range want {
		if u, p := providers[id].gitAuth(conn, "tok"); u != w[0] || p != w[1] {
			t.Fatalf("%s 的 git 认证应是 %v，实际 %s:%s", id, w, u, p)
		}
	}
	if providers["gitlab"].API != "https://git.corp.example/api/v4" || providers["gitea"].Name != "Codeberg" {
		t.Fatalf("自建 GitLab 的 API 按地址推出，Gitea 默认是 Codeberg：%s %s", providers["gitlab"].API, providers["gitea"].Name)
	}
	cases := []struct {
		id, raw, repo string
		ok            bool
	}{
		{"github", "https://github.com/octo/demo.git", "octo/demo", true},
		{"github", "https://github.com/octo/demo/extra", "", false},
		{"github", "https://gitlab.com/octo/demo", "", false},
		{"gitlab", "https://git.corp.example/group/sub/demo.git", "group/sub/demo", true},
		{"gitee", "https://gitee.com/li-lei/demo", "li-lei/demo", true},
		{"bitbucket", "http://bitbucket.org/team/demo.git", "", false},
		{"google", "https://accounts.google.com/a/b", "", false},
	}
	for _, tc := range cases {
		if repo, ok := providers[tc.id].repoOf(tc.raw); repo != tc.repo || ok != tc.ok {
			t.Fatalf("%s %s：应为 %q %v，实际 %q %v", tc.id, tc.raw, tc.repo, tc.ok, repo, ok)
		}
	}
}

// 仓库列表一页一页翻：某页不满（Bitbucket 是没有 next）就停，最多 repoPages 页。
func TestRepoListsPageThrough(t *testing.T) {
	cfg := Config{Providers: map[string]ProviderConfig{}}
	for _, id := range []string{"github", "gitee", "gitlab", "gitea", "bitbucket"} {
		cfg.Providers[id] = ProviderConfig{ClientID: "c", ClientSecret: "s"}
	}
	providers := newProviders(cfg)
	for _, tc := range []struct {
		id                 string
		total, want, pages int
	}{
		{"github", 230, 230, 3},
		{"github", 200, 200, 3}, // 第二页正好满：再问一页，是空的
		{"github", 1000, 500, 5},
		{"gitea", 120, 120, 3},
		{"gitea", 1000, 250, 5},
		{"gitee", 150, 150, 2},
		{"gitlab", 99, 99, 1},
		{"bitbucket", 230, 230, 3},
		{"bitbucket", 1000, 500, 5},
	} {
		var asked []string
		fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			q := r.URL.Query()
			asked = append(asked, q.Get("page"))
			size := 100
			if tc.id == "gitea" {
				size = 50
			}
			page := 1
			fmt.Sscan(q.Get("page"), &page)
			items := []map[string]any{}
			for i := (page - 1) * size; i < page*size && i < tc.total; i++ {
				name := fmt.Sprintf("o/r%d", i)
				items = append(items, map[string]any{"full_name": name, "path_with_namespace": name, "html_url": "https://x/" + name, "clone_url": "https://x/" + name + ".git"})
			}
			w.Header().Set("Content-Type", "application/json")
			if tc.id == "bitbucket" {
				res := map[string]any{"values": items}
				if page*size < tc.total {
					res["next"] = fmt.Sprintf("%s/repositories?page=%d", "https://api.bitbucket.org/2.0", page+1)
				}
				_ = json.NewEncoder(w).Encode(res)
				return
			}
			_ = json.NewEncoder(w).Encode(items)
		}))
		p := *providers[tc.id]
		p.API, p.Web = fake.URL, fake.URL
		repos, err := p.repos(t.Context(), "tok")
		fake.Close()
		if err != nil || len(repos) != tc.want || len(asked) != tc.pages {
			t.Fatalf("%s 共 %d 个：应取到 %d 个、问 %d 页，实际 %d 个、问了 %v（%v）", tc.id, tc.total, tc.want, tc.pages, len(repos), asked, err)
		}
		if repos[0].FullName != "o/r0" || repos[len(repos)-1].FullName != fmt.Sprintf("o/r%d", tc.want-1) || repos[0].Provider != tc.id {
			t.Fatalf("%s：顺序或来源不对：%v … %v", tc.id, repos[0], repos[len(repos)-1])
		}
	}
}
