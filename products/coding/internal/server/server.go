// Package server 是 coding-web 的 HTTP 服务：JSON API 与内嵌的网页。
//
// 租户与用户只来自登录得到的 Runtime 令牌（放在 HttpOnly Cookie 里），每个请求都重新校验签名与过期时间；
// 项目与任务的元数据按租户分目录存放，工作区路径一律由服务端按（租户、项目 id）拼出，请求给不出别的路径。
// 改写操作要带 X-Coding-Web 头（配合 SameSite Cookie 防跨站请求）。
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/products/coding/internal/web"
)

const (
	cookieName  = "cw_token"
	csrfHeader  = "X-Coding-Web"
	tokenTTL    = 12 * time.Hour
	maxBodySize = 1 << 20
)

// slug 与 Runtime 的租户、workspace 格式一致（libs/go-core/dev/localingress、Worker 的工作区工具）。
var slug = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// Config 是 coding-web 的配置，见 cmd/coding-web 的说明。
type Config struct {
	TaskAPI       string
	WorkspaceRoot string
	DataDir       string
	DevLogin      bool
	SigningKey    []byte
	PoliciesFile  string
	Git           string
	SecureCookie  bool
	// 登录方式（github、gitee、gitlab、gitea、bitbucket、google，见 oauth.go）：Client ID 与 Secret 都给了才打开；
	// PublicURL 用来拼回调地址 <PublicURL>/api/auth/<平台>/callback。GitHub 也可以用下面旧的 GitHub* 字段配置。
	Providers          map[string]ProviderConfig
	GitHubClientID     string
	GitHubClientSecret string
	GitHubURL          string
	GitHubAPI          string
	GitHubScopes       string
	PublicURL          string
	// Worker 的 shell_run 在哪里跑（docker 或 host）与 docker 时的镜像；只用来在界面上说明环境。
	Sandbox      string
	SandboxImage string
}

// Server 是 coding-web。
type Server struct {
	cfg       Config
	ingress   *localingress.Provider
	store     *Store
	api       *TaskAPI
	git       Git
	providers map[string]*Provider
	// 按租户串行“改写租户策略、提交任务”，让每个任务固定的是它自己选的沙箱等级。
	tenantLocks sync.Map
}

// New 校验配置并构造 Server。
func New(cfg Config) (*Server, error) {
	switch {
	case cfg.TaskAPI == "":
		return nil, errors.New("缺少 CODING_TASK_API")
	case cfg.WorkspaceRoot == "":
		return nil, errors.New("缺少 CODING_WORKSPACE_ROOT")
	case cfg.DataDir == "":
		return nil, errors.New("缺少 CODING_DATA_DIR")
	case len(cfg.SigningKey) == 0:
		return nil, errors.New("缺少 AR_DEV_SIGNING_KEY（校验与签发 Runtime 令牌）")
	}
	if cfg.Git == "" {
		cfg.Git = "git"
	}
	for _, dir := range []string{cfg.WorkspaceRoot, cfg.DataDir} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
	}
	hooks := filepath.Join(cfg.DataDir, "empty-hooks")
	if err := os.MkdirAll(hooks, 0o755); err != nil {
		return nil, err
	}
	providers := newProviders(cfg)
	if len(providers) > 0 && cfg.PublicURL == "" {
		return nil, errors.New("打开第三方登录要给 CODING_PUBLIC_URL（拼 OAuth 回调地址）")
	}
	return &Server{
		cfg:       cfg,
		ingress:   localingress.New(localingress.Options{SigningKey: cfg.SigningKey, Limit: 1 << 30, Window: time.Minute}),
		store:     NewStore(cfg.DataDir),
		api:       NewTaskAPI(strings.TrimRight(cfg.TaskAPI, "/")),
		git:       Git{Bin: cfg.Git, HooksDir: hooks},
		providers: providers,
	}, nil
}

// Handler 返回挂好全部路由的 http.Handler。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok")) })
	mux.HandleFunc("GET /api/config", s.handleConfig)
	mux.HandleFunc("POST /api/login", s.handleLogin)
	mux.HandleFunc("POST /api/logout", s.handleLogout)
	mux.HandleFunc("GET /api/me", s.authed(s.handleMe))
	// 第三方登录与账号关联（accounts.go）。
	mux.HandleFunc("GET /api/auth/{provider}/login", s.handleOAuthLogin)
	mux.HandleFunc("GET /api/auth/{provider}/callback", s.handleOAuthCallback)
	mux.HandleFunc("GET /api/account", s.authed(s.handleAccount))
	mux.HandleFunc("POST /api/account/connections/{provider}/disconnect", s.authed(s.handleDisconnect))
	mux.HandleFunc("GET /api/git/{provider}/repos", s.authed(s.handleRepos))
	mux.HandleFunc("GET /api/github/repos", s.authed(s.handleRepos))

	mux.HandleFunc("GET /api/samples", s.authed(s.handleSamples))
	// 工作区（项目）：基线仓库。
	mux.HandleFunc("GET /api/projects", s.authed(s.handleListProjects))
	mux.HandleFunc("POST /api/projects", s.authed(s.handleCreateProject))
	mux.HandleFunc("DELETE /api/projects/{project}", s.authed(s.handleDeleteProject))
	// 会话：一段对话加一份工作副本；文件、改动、提交、推送与任务都按会话。
	mux.HandleFunc("GET /api/sessions", s.authed(s.handleListSessions))
	mux.HandleFunc("POST /api/projects/{project}/sessions", s.authed(s.handleCreateSession))
	const sess = "/api/projects/{project}/sessions/{session}"
	mux.HandleFunc("DELETE "+sess, s.authed(s.handleDeleteSession))
	mux.HandleFunc("POST "+sess+"/rename", s.authed(s.handleRenameSession))
	mux.HandleFunc("GET "+sess+"/env", s.authed(s.handleEnv))
	mux.HandleFunc("GET "+sess+"/tree", s.authed(s.handleTree))
	mux.HandleFunc("GET "+sess+"/file", s.authed(s.handleFile))
	mux.HandleFunc("GET "+sess+"/log", s.authed(s.handleLog))
	mux.HandleFunc("GET "+sess+"/changes", s.authed(s.handleChanges))
	mux.HandleFunc("GET "+sess+"/patch", s.authed(s.handlePatch))
	mux.HandleFunc("POST "+sess+"/commit", s.authed(s.handleCommit))
	mux.HandleFunc("POST "+sess+"/discard", s.authed(s.handleDiscard))
	mux.HandleFunc("POST "+sess+"/publish", s.authed(s.handlePublish))
	mux.HandleFunc("POST "+sess+"/reset-branch", s.authed(s.handleResetBranch))
	mux.HandleFunc("GET "+sess+"/tasks", s.authed(s.handleListTasks))
	mux.HandleFunc("POST "+sess+"/tasks", s.authed(s.handleSubmitTask))
	mux.HandleFunc("GET "+sess+"/tasks/{task}", s.authed(s.handleQueryTask))
	mux.HandleFunc("GET "+sess+"/tasks/{task}/events", s.authed(s.handleEvents))
	mux.HandleFunc("POST "+sess+"/tasks/{task}/reply", s.authed(s.handleReply))
	mux.HandleFunc("POST "+sess+"/tasks/{task}/cancel", s.authed(s.handleCancel))
	mux.HandleFunc("GET /api/tasks/active", s.authed(s.handleActiveTasks))

	mux.Handle("GET /", web.Handler())
	return securityHeaders(mux)
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("X-Frame-Options", "DENY")
		h.Set("Referrer-Policy", "same-origin")
		// 头像来自各家平台（GitHub、Gitee、GitLab、Gravatar、Google…），图片放开 https；脚本、样式、连接仍只许同源。
		h.Set("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; connect-src 'self'")
		next.ServeHTTP(w, r)
	})
}

// --- 认证 ---

type identity struct {
	ingress.Identity
	token string
}

type authedHandler func(w http.ResponseWriter, r *http.Request, id identity)

func (s *Server) authed(h authedHandler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cookie, err := r.Cookie(cookieName)
		if err != nil || cookie.Value == "" {
			writeError(w, http.StatusUnauthorized, "请先登录")
			return
		}
		id, err := s.ingress.Authenticate(r.Context(), ingress.Credentials{BearerToken: cookie.Value})
		if err != nil {
			s.clearCookie(w)
			writeError(w, http.StatusUnauthorized, "登录已过期，请重新登录")
			return
		}
		if r.Method != http.MethodGet && r.Header.Get(csrfHeader) == "" {
			writeError(w, http.StatusForbidden, "缺少 X-Coding-Web 头")
			return
		}
		h(w, r, identity{Identity: id, token: cookie.Value})
	}
}

func (s *Server) handleConfig(w http.ResponseWriter, _ *http.Request) {
	// 全部登录方式都列出来（没配置的 configured 为 false，开发环境的登录页用它说明还能接哪些）。
	providers := []map[string]any{}
	for _, sp := range providerSpecs {
		item := map[string]any{"id": sp.id, "name": sp.name, "git": sp.git, "configured": false, "url": sp.web}
		if p := s.providers[sp.id]; p != nil {
			item["name"], item["configured"], item["url"] = p.Name, true, p.Web
			if p.git {
				item["request_name"] = p.requestName()
			}
		}
		providers = append(providers, item)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"dev_login": s.cfg.DevLogin, "github_login": s.providers["github"] != nil, "providers": providers,
		// 有开发租户策略文件才能按任务设沙箱等级（见 runsettings.go）。
		"sandbox_control": s.cfg.PoliciesFile != "", "sandbox": s.cfg.Sandbox,
	})
}

// setSession 签发 Runtime 令牌，放进 HttpOnly Cookie。
func (s *Server) setSession(w http.ResponseWriter, tenant, user string) {
	token := s.ingress.IssueToken(tenant, user, tokenTTL)
	http.SetCookie(w, &http.Cookie{
		Name: cookieName, Value: token, Path: "/", HttpOnly: true, Secure: s.cfg.SecureCookie,
		SameSite: http.SameSiteLaxMode, MaxAge: int(tokenTTL.Seconds()),
	})
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	if !s.cfg.DevLogin {
		writeError(w, http.StatusForbidden, "没有打开开发登录（CODING_DEV_LOGIN=1）")
		return
	}
	if r.Header.Get(csrfHeader) == "" {
		writeError(w, http.StatusForbidden, "缺少 X-Coding-Web 头")
		return
	}
	var req struct {
		Tenant string `json:"tenant"`
		User   string `json:"user"`
	}
	if !readJSON(w, r, &req) {
		return
	}
	req.Tenant, req.User = strings.ToLower(strings.TrimSpace(req.Tenant)), strings.ToLower(strings.TrimSpace(req.User))
	if !slug.MatchString(req.Tenant) || !slug.MatchString(req.User) {
		writeError(w, http.StatusBadRequest, "租户与用户名只能用小写字母、数字、- 与 _，以字母或数字开头")
		return
	}
	if reservedTenant(req.Tenant) {
		writeError(w, http.StatusBadRequest, "gh-、gitee-、gl-、gitea-、bb-、google- 开头的租户留给第三方登录")
		return
	}
	if err := s.ensureTenantPolicy(req.Tenant); err != nil {
		writeError(w, http.StatusInternalServerError, "写租户策略失败："+err.Error())
		return
	}
	s.setSession(w, req.Tenant, req.User)
	writeJSON(w, http.StatusOK, map[string]string{"tenant": req.Tenant, "user": req.User})
}

func (s *Server) handleLogout(w http.ResponseWriter, _ *http.Request) {
	s.clearCookie(w)
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (s *Server) clearCookie(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{Name: cookieName, Value: "", Path: "/", HttpOnly: true, Secure: s.cfg.SecureCookie, SameSite: http.SameSiteLaxMode, MaxAge: -1})
}

func (s *Server) handleMe(w http.ResponseWriter, _ *http.Request, id identity) {
	me := map[string]any{"tenant": id.TenantID, "user": id.UserID, "expires_at": id.ExpiresAt}
	a, conns := s.connections(id)
	me["connections"] = conns
	for _, c := range conns {
		if c.Provider == a.Primary || me["avatar_url"] == nil {
			me["name"], me["avatar_url"], me["login"] = c.DisplayName, c.AvatarURL, c.Login
		}
		if c.Provider == "github" { // 旧的网页与测试读这个
			me["github"] = map[string]any{"login": c.Login, "name": c.DisplayName, "avatar_url": c.AvatarURL}
		}
	}
	writeJSON(w, http.StatusOK, me)
}

// ensureTenantPolicy 在开发租户策略文件里给新租户 write_reversible 的 effect 上限（改代码、跑测试要用）；
// 已经列出的租户不动。task-service 每次提交都重新读这个文件。
func (s *Server) ensureTenantPolicy(tenant string) error {
	return s.writeTenantCeiling(tenant, defaultSandbox, true)
}

// writeTenantCeiling 把租户的 effect 上限写进开发租户策略文件；onlyIfMissing 时已经列出的租户不动。
func (s *Server) writeTenantCeiling(tenant, ceiling string, onlyIfMissing bool) error {
	if s.cfg.PoliciesFile == "" {
		return nil
	}
	s.store.mu.Lock()
	defer s.store.mu.Unlock()
	doc, err := s.readPolicies()
	if err != nil {
		return err
	}
	tenants, _ := doc["tenants"].(map[string]any)
	if tenants == nil {
		tenants = map[string]any{}
	}
	entry, ok := tenants[tenant].(map[string]any)
	if ok && onlyIfMissing {
		return nil
	}
	if entry == nil {
		entry = map[string]any{}
	}
	if entry["effect_ceiling"] == ceiling {
		return nil
	}
	entry["effect_ceiling"] = ceiling
	tenants[tenant] = entry
	doc["tenants"] = tenants
	return writeFileAtomic(s.cfg.PoliciesFile, doc)
}

func (s *Server) readPolicies() (map[string]any, error) {
	doc := map[string]any{}
	data, err := os.ReadFile(s.cfg.PoliciesFile)
	if errors.Is(err, os.ErrNotExist) {
		return doc, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("租户策略文件格式不对：%w", err)
	}
	return doc, nil
}

// tenantCeiling 是策略文件里这个租户现在的 effect 上限；读不到时为空。
func (s *Server) tenantCeiling(tenant string) string {
	if s.cfg.PoliciesFile == "" {
		return ""
	}
	s.store.mu.Lock()
	defer s.store.mu.Unlock()
	doc, err := s.readPolicies()
	if err != nil {
		return ""
	}
	tenants, _ := doc["tenants"].(map[string]any)
	entry, _ := tenants[tenant].(map[string]any)
	ceiling, _ := entry["effect_ceiling"].(string)
	return ceiling
}

// --- 小工具 ---

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func readJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodySize)
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法的 JSON")
		return false
	}
	return true
}

// project 取出请求路径里的项目，确认它属于这个租户。
func (s *Server) project(w http.ResponseWriter, r *http.Request, id identity) (Project, bool) {
	pid := r.PathValue("project")
	if !slug.MatchString(pid) {
		writeError(w, http.StatusNotFound, "项目不存在")
		return Project{}, false
	}
	p, ok, err := s.store.Project(id.TenantID, pid)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return Project{}, false
	}
	if !ok {
		writeError(w, http.StatusNotFound, "项目不存在")
		return Project{}, false
	}
	return p, true
}

func (s *Server) workspace(tenant, project string) string {
	return filepath.Join(s.cfg.WorkspaceRoot, tenant, project)
}

func ctxTimeout(r *http.Request, d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(r.Context(), d)
}
