package server

import (
	"encoding/json"
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

// fakeGitHub 是 OAuth 与 REST API 的替身：记下收到的 PR 请求与令牌。
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

// githubLogin 走一遍 OAuth：拿到 state、带着授权码回调，返回登录后的客户端。
func githubLoginFlow(t *testing.T, ts *httptest.Server, code string) (*client, *http.Response) {
	t.Helper()
	c := newClient(t, ts.URL)
	c.http.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	res, err := c.http.Get(ts.URL + "/api/auth/github/login")
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != http.StatusFound {
		t.Fatalf("登录入口应 302 到 GitHub，实际 %d", res.StatusCode)
	}
	loc, _ := url.Parse(res.Header.Get("Location"))
	if loc.Path != "/login/oauth/authorize" || loc.Query().Get("redirect_uri") != ts.URL+"/api/auth/github/callback" {
		t.Fatalf("授权地址不对：%s", loc)
	}
	state := loc.Query().Get("state")
	res, err = c.http.Get(ts.URL + "/api/auth/github/callback?code=" + code + "&state=" + state)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	return c, res
}

func gitIn(t *testing.T, s *Server, dir string, args ...string) string {
	t.Helper()
	out, err := s.git.RunAs(t.Context(), dir, "t", "t@example.com", args...)
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(out)
}

func TestGitHubLoginPublishAndReset(t *testing.T) {
	gh := newFakeGitHub(t)
	var ts *httptest.Server
	ts, _, cfg, srv := newServerWith(t, func(c *Config) {
		c.GitHubClientID, c.GitHubClientSecret = "client", "secret"
		c.GitHubURL, c.GitHubAPI = "http://placeholder", "http://placeholder"
		c.PublicURL = "http://placeholder"
	})
	srv.git.AllowFileProtocol = true
	srv.gh.Web, srv.gh.API, srv.cfg.PublicURL = gh.server.URL, gh.server.URL, ts.URL

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
	if code, _, raw := c.do("GET", "/api/github/repos", nil); code != http.StatusOK || !strings.Contains(raw, "octo-cat/demo") {
		t.Fatalf("仓库列表：%d %s", code, raw)
	}

	// 项目：示例仓库 + 本地裸仓库作 GitHub 远端。
	_, project, _ := c.do("POST", "/api/projects", map[string]string{"name": "demo", "sample": "ci_rounding"})
	pid := project["id"].(string)
	ws := filepath.Join(cfg.WorkspaceRoot, "gh-octo-cat", pid)
	bare := filepath.Join(t.TempDir(), "demo.git")
	gitIn(t, srv, filepath.Dir(bare), "init", "-q", "--bare", "-b", "main", bare)
	gitIn(t, srv, ws, "remote", "add", "origin", bare)
	gitIn(t, srv, ws, "push", "-q", "origin", "main")
	gitIn(t, srv, ws, "fetch", "-q", "origin")
	if _, err := srv.updateProject("gh-octo-cat", pid, func(p *Project) { p.GitHubRepo, p.BaseBranch = "octo-cat/demo", "main" }); err != nil {
		t.Fatal(err)
	}

	if err := os.WriteFile(filepath.Join(ws, "pricing.py"), []byte("# fixed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	code, out, raw := c.do("POST", "/api/projects/"+pid+"/publish", map[string]string{"title": "Fix rounding"})
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

	if code, _, _ := c.do("POST", "/api/projects/"+pid+"/publish", map[string]string{"title": "again"}); code != http.StatusBadRequest {
		t.Fatalf("没有新改动时应 400，实际 %d", code)
	}
	_ = os.WriteFile(filepath.Join(ws, "notes.md"), []byte("more\n"), 0o644)
	code, out, raw = c.do("POST", "/api/projects/"+pid+"/publish", map[string]string{"title": "More changes"})
	if code != http.StatusOK || out["created"] != false || out["branch"] != branch || len(gh.pulls) != 1 {
		t.Fatalf("再推应只更新同一个 PR：%d %s", code, raw)
	}

	code, out, raw = c.do("POST", "/api/projects/"+pid+"/reset-branch", nil)
	if code != http.StatusOK || out["project"].(map[string]any)["branch"] != nil {
		t.Fatalf("回到基线分支：%d %s", code, raw)
	}
	if cur := srv.git.Branch(t.Context(), ws); cur != "main" {
		t.Fatalf("应回到 main，实际 %s", cur)
	}
}

func TestDevLoginCannotTakeGitHubTenants(t *testing.T) {
	ts, _, _ := newServer(t)
	c := newClient(t, ts.URL)
	if code, _, _ := c.do("POST", "/api/login", map[string]string{"tenant": "gh-octo-cat", "user": "octo-cat"}); code != http.StatusBadRequest {
		t.Fatalf("开发登录不能用 gh- 开头的租户，实际 %d", code)
	}
}

func TestPublishNeedsAGitHubProjectAndAccount(t *testing.T) {
	ts, _, _ := newServer(t)
	c := newClient(t, ts.URL)
	c.do("POST", "/api/login", map[string]string{"tenant": "acme", "user": "alice"})
	_, project, _ := c.do("POST", "/api/projects", map[string]string{"name": "demo", "sample": "ci_rounding"})
	if code, _, _ := c.do("POST", "/api/projects/"+project["id"].(string)+"/publish", map[string]string{"title": "x"}); code != http.StatusBadRequest {
		t.Fatalf("不是 GitHub 项目不能推送，实际 %d", code)
	}
}
