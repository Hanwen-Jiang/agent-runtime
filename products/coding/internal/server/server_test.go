package server

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// fakeTaskAPI 记下收到的 SubmitTask（与那一刻租户策略文件的内容），对 QueryTask 回 status（默认 DONE）与两个 Round 的花费。
// 没带 sessionId 的提交开一个新的 Runtime Session（ses_<序号>），带了就沿用。
type fakeTaskAPI struct {
	submits  []map[string]any
	tokens   []string
	queries  int
	status   string
	policies string
	// 每次 SubmitTask 时策略文件的内容（task-service 在提交那一刻读它）。
	seenPolicies []string
}

func (f *fakeTaskAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" {
		_, _ = io.WriteString(w, "ok")
		return
	}
	f.tokens = append(f.tokens, r.Header.Get("Authorization"))
	var body map[string]any
	_ = json.NewDecoder(r.Body).Decode(&body)
	w.Header().Set("Content-Type", "application/json")
	switch {
	case strings.HasSuffix(r.URL.Path, "/SubmitTask"):
		f.submits = append(f.submits, body)
		data, _ := os.ReadFile(f.policies)
		f.seenPolicies = append(f.seenPolicies, string(data))
		n := strconv.Itoa(len(f.submits))
		session, _ := body["sessionId"].(string)
		if session == "" {
			session = "ses_" + n
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"taskId": "tsk_" + n, "sessionId": session, "eventsUrl": "http://x/v1/tasks/tsk_" + n + "/events"})
	case strings.HasSuffix(r.URL.Path, "/QueryTask"):
		f.queries++
		status := f.status
		if status == "" {
			status = "TASK_STATUS_DONE"
		}
		state := map[string]any{"value": map[string]any{"rounds": []any{map[string]any{"seq": 1, "cost_usd": 0.1}, map[string]any{"seq": 2, "cost_usd": 0.2}}}}
		_ = json.NewEncoder(w).Encode(map[string]any{"taskId": body["taskId"], "status": status, "taskState": state})
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

type client struct {
	t    *testing.T
	base string
	http *http.Client
}

func newClient(t *testing.T, base string) *client {
	jar, _ := cookiejar.New(nil)
	return &client{t: t, base: base, http: &http.Client{Jar: jar}}
}

func (c *client) do(method, path string, body any) (int, map[string]any, string) {
	c.t.Helper()
	var reader io.Reader
	if body != nil {
		data, _ := json.Marshal(body)
		reader = strings.NewReader(string(data))
	}
	req, _ := http.NewRequest(method, c.base+path, reader)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(csrfHeader, "1")
	res, err := c.http.Do(req)
	if err != nil {
		c.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	var obj map[string]any
	_ = json.Unmarshal(raw, &obj)
	return res.StatusCode, obj, string(raw)
}

func newServer(t *testing.T) (*httptest.Server, *fakeTaskAPI, Config) {
	ts, fake, cfg, _ := newServerWith(t, nil)
	return ts, fake, cfg
}

func newServerWith(t *testing.T, edit func(*Config)) (*httptest.Server, *fakeTaskAPI, Config, *Server) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("PATH 上没有 git")
	}
	fake := &fakeTaskAPI{}
	api := httptest.NewServer(fake)
	t.Cleanup(api.Close)
	dir := t.TempDir()
	cfg := Config{
		TaskAPI: api.URL, WorkspaceRoot: filepath.Join(dir, "ws"), DataDir: filepath.Join(dir, "data"),
		DevLogin: true, SigningKey: []byte("test-key"), PoliciesFile: filepath.Join(dir, "policies.json"),
	}
	if edit != nil {
		edit(&cfg)
	}
	fake.policies = cfg.PoliciesFile
	srv, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return ts, fake, cfg, srv
}

// sampleProject 登录 acme/alice 并从示例建一个工作区，返回工作区 id。
func sampleProject(t *testing.T, c *client) string {
	t.Helper()
	c.do("POST", "/api/login", map[string]string{"tenant": "acme", "user": "alice"})
	code, project, raw := c.do("POST", "/api/projects", map[string]string{"name": "Demo", "sample": "ci_rounding"})
	if code != http.StatusCreated {
		t.Fatalf("建工作区失败：%d %s", code, raw)
	}
	return project["id"].(string)
}

// newSession 在工作区里开一个会话，返回会话的接口前缀（/api/projects/<p>/sessions/<s>）与工作副本目录。
func newSession(t *testing.T, c *client, cfg Config, tenant, pid string) (string, string) {
	t.Helper()
	code, sess, raw := c.do("POST", "/api/projects/"+pid+"/sessions", map[string]string{"title": "试一试"})
	if code != http.StatusCreated {
		t.Fatalf("开会话失败：%d %s", code, raw)
	}
	return "/api/projects/" + pid + "/sessions/" + sess["id"].(string), filepath.Join(cfg.WorkspaceRoot, tenant, sess["workspace_id"].(string))
}

func TestLoginWorkspaceSessionChangesCommitAndTask(t *testing.T) {
	ts, fake, cfg := newServer(t)
	c := newClient(t, ts.URL)

	if code, _, _ := c.do("GET", "/api/projects", nil); code != http.StatusUnauthorized {
		t.Fatalf("没登录应 401，实际 %d", code)
	}
	if code, _, raw := c.do("POST", "/api/login", map[string]string{"tenant": "acme", "user": "alice"}); code != http.StatusOK {
		t.Fatalf("登录失败：%d %s", code, raw)
	}
	policies, _ := os.ReadFile(cfg.PoliciesFile)
	if !strings.Contains(string(policies), `"acme"`) || !strings.Contains(string(policies), "write_reversible") {
		t.Fatalf("新租户的 effect 上限应写进策略文件：%s", policies)
	}

	pid := sampleProject(t, c)
	base := filepath.Join(cfg.WorkspaceRoot, "acme", pid)
	if _, err := os.Stat(filepath.Join(base, "pricing.py")); err != nil {
		t.Fatalf("示例文件应在基线仓库里：%v", err)
	}
	api, ws := newSession(t, c, cfg, "acme", pid)
	if ws == base || !strings.HasPrefix(filepath.Base(ws), pid+"--") {
		t.Fatalf("会话应有自己的工作副本（<工作区>--<会话>）：%s", ws)
	}
	if _, err := os.Stat(filepath.Join(ws, ".git", "HEAD")); err != nil {
		t.Fatalf("工作副本应是完整的仓库（.git 是目录，Docker 沙箱里也能用 git）：%v", err)
	}

	if err := os.WriteFile(filepath.Join(ws, "new.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, changes, _ := c.do("GET", api+"/changes", nil)
	if files, _ := changes["files"].([]any); len(files) != 1 || !strings.Contains(changes["diff"].(string), "+hello") {
		t.Fatalf("改动里应有新文件：%v", changes)
	}
	if code, _, raw := c.do("POST", api+"/commit", map[string]string{"message": "add new.txt"}); code != http.StatusOK {
		t.Fatalf("提交失败：%d %s", code, raw)
	}
	_, changes, _ = c.do("GET", api+"/changes", nil)
	if files, _ := changes["files"].([]any); len(files) != 0 {
		t.Fatalf("提交之后不应再有改动：%v", changes)
	}
	if _, err := os.Stat(filepath.Join(base, "new.txt")); err == nil {
		t.Fatal("会话里的改动不应出现在基线仓库里")
	}

	code, task, raw := c.do("POST", api+"/tasks", map[string]string{"message": "fix the tests\nand explain"})
	if code != http.StatusCreated || task["id"] != "tsk_1" {
		t.Fatalf("提交任务失败：%d %s", code, raw)
	}
	if len(fake.submits) != 1 || fake.submits[0]["workspaceId"] != filepath.Base(ws) || fake.submits[0]["sessionAccess"] != "SESSION_ACCESS_READ_WRITE" {
		t.Fatalf("SubmitTask 的 workspace 应是会话的工作副本：%v", fake.submits)
	}
	if !strings.HasPrefix(fake.tokens[0], "Bearer ") {
		t.Fatal("要把令牌转给 Task API")
	}
	if code, _, raw := c.do("GET", api+"/tasks", nil); code != http.StatusOK || !strings.Contains(raw, "TASK_STATUS_DONE") {
		t.Fatalf("任务列表应按 Runtime 刷新到 DONE：%d %s", code, raw)
	}

	// 别的租户看不到这个工作区与会话。
	other := newClient(t, ts.URL)
	other.do("POST", "/api/login", map[string]string{"tenant": "globex", "user": "bob"})
	if code, _, _ := other.do("GET", api+"/changes", nil); code != http.StatusNotFound {
		t.Fatalf("别的租户访问应 404，实际 %d", code)
	}
	if _, _, raw := other.do("GET", "/api/sessions", nil); strings.TrimSpace(raw) != "[]" {
		t.Fatalf("别的租户的会话列表应为空：%s", raw)
	}
}

func TestSessionsHaveTheirOwnCopiesAndConversations(t *testing.T) {
	ts, fake, cfg := newServer(t)
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)
	apiA, wsA := newSession(t, c, cfg, "acme", pid)
	apiB, wsB := newSession(t, c, cfg, "acme", pid)
	if wsA == wsB {
		t.Fatal("两个会话的工作副本应不同")
	}

	if err := os.WriteFile(filepath.Join(wsA, "pricing.py"), []byte("# changed in A\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, changesB, _ := c.do("GET", apiB+"/changes", nil)
	if files, _ := changesB["files"].([]any); len(files) != 0 {
		t.Fatalf("会话 A 的改动不应出现在会话 B：%v", changesB)
	}

	c.do("POST", apiA+"/tasks", map[string]string{"message": "第一件事"})
	c.do("POST", apiB+"/tasks", map[string]string{"message": "另一件事"})
	c.do("POST", apiA+"/tasks", map[string]string{"message": "接着第一件事"})
	got := func(i int, key string) any { return fake.submits[i][key] }
	if got(0, "workspaceId") == got(1, "workspaceId") {
		t.Fatal("不同会话提交到不同的 workspace")
	}
	if got(0, "sessionId") != nil || got(1, "sessionId") != nil || got(2, "sessionId") != "ses_1" {
		t.Fatalf("每个会话各自一个 Runtime Session，之后的任务沿用它：%v", fake.submits)
	}

	_, _, raw := c.do("GET", "/api/sessions", nil)
	var sessions []Session
	if err := json.Unmarshal([]byte(raw), &sessions); err != nil || len(sessions) != 2 {
		t.Fatalf("会话列表：%v %s", err, raw)
	}
	if sessions[0].Title != "试一试" || sessions[0].RuntimeSessionID != "ses_1" || sessions[1].RuntimeSessionID != "ses_2" {
		t.Fatalf("会话按最近活动排序，记下各自的 Runtime Session：%+v", sessions)
	}
	if _, _, raw := c.do("GET", apiB+"/tasks", nil); strings.Contains(raw, "第一件事") {
		t.Fatalf("会话 B 的任务列表里不应有会话 A 的任务：%s", raw)
	}

	if code, renamed, _ := c.do("POST", apiB+"/rename", map[string]string{"title": "  另一件事  "}); code != http.StatusOK || renamed["title"] != "另一件事" {
		t.Fatalf("改名：%d %v", code, renamed)
	}
}

func TestUntitledSessionsAreNamedAfterTheirFirstTask(t *testing.T) {
	ts, _, _ := newServer(t)
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)
	code, sess, _ := c.do("POST", "/api/projects/"+pid+"/sessions", map[string]string{})
	if code != http.StatusCreated || sess["title"] != "" {
		t.Fatalf("开没有标题的会话：%d %v", code, sess)
	}
	api := "/api/projects/" + pid + "/sessions/" + sess["id"].(string)
	c.do("POST", api+"/tasks", map[string]string{"message": strings.Repeat("修", 50) + "\n第二行"})
	_, _, raw := c.do("GET", "/api/sessions", nil)
	if !strings.Contains(raw, strings.Repeat("修", 40)+"…") {
		t.Fatalf("标题取第一行、最多 40 个字：%s", raw)
	}
}

func TestLegacyProjectsBecomeAMainSession(t *testing.T) {
	ts, fake, cfg := newServer(t)
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)
	// 模拟会话出现之前的数据：项目上记着 Runtime Session 与推送记录，任务记在 tasks-<项目>.json。
	created := time.Date(2026, 10, 24, 8, 0, 0, 0, time.UTC)
	store := NewStore(cfg.DataDir)
	if err := store.UpdateProjects("acme", func(ps []Project) ([]Project, error) {
		ps[0].SessionID, ps[0].Branch, ps[0].PRNumber = "ses_old", "agent/fix-1234", 3
		return ps, nil
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.UpdateTasks("acme", pid, func([]TaskRecord) []TaskRecord {
		return []TaskRecord{{ID: "tsk_old", Message: "修好 CI\n细节", CreatedAt: created, Status: "TASK_STATUS_DONE", Settled: true}}
	}); err != nil {
		t.Fatal(err)
	}

	_, _, raw := c.do("GET", "/api/sessions", nil)
	var sessions []Session
	_ = json.Unmarshal([]byte(raw), &sessions)
	if len(sessions) != 1 {
		t.Fatalf("旧项目应迁移成一个会话：%s", raw)
	}
	main := sessions[0]
	if main.ID != "main" || main.WorkspaceID != pid || main.RuntimeSessionID != "ses_old" || main.Branch != "agent/fix-1234" || main.PRNumber != 3 || main.Title != "修好 CI" {
		t.Fatalf("main 会话沿用旧项目的目录、Session 与推送记录：%+v", main)
	}
	api := "/api/projects/" + pid + "/sessions/main"
	if _, _, raw := c.do("GET", api+"/tasks", nil); !strings.Contains(raw, "tsk_old") {
		t.Fatalf("旧任务应在 main 会话里：%s", raw)
	}
	c.do("POST", api+"/tasks", map[string]string{"message": "继续"})
	if fake.submits[0]["workspaceId"] != pid || fake.submits[0]["sessionId"] != "ses_old" {
		t.Fatalf("main 会话的任务沿用旧的 workspace 与 Session：%v", fake.submits[0])
	}
	newSession(t, c, cfg, "acme", pid)
	_, _, raw = c.do("GET", "/api/sessions", nil)
	if strings.Count(raw, `"project_id"`) != 2 {
		t.Fatalf("开新会话后 main 会话还在：%s", raw)
	}
}

func TestDeletingSessionsAndWorkspaces(t *testing.T) {
	ts, fake, cfg := newServer(t)
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)
	apiA, wsA := newSession(t, c, cfg, "acme", pid)
	_, wsB := newSession(t, c, cfg, "acme", pid)

	fake.status = "TASK_STATUS_RUNNING"
	c.do("POST", apiA+"/tasks", map[string]string{"message": "长任务"})
	if code, _, _ := c.do("DELETE", apiA, nil); code != http.StatusConflict {
		t.Fatalf("有任务在跑的会话不能删，实际 %d", code)
	}
	fake.status = "TASK_STATUS_DONE"
	if code, _, raw := c.do("DELETE", apiA, nil); code != http.StatusOK {
		t.Fatalf("删会话：%d %s", code, raw)
	}
	if _, err := os.Stat(wsA); !os.IsNotExist(err) {
		t.Fatal("删会话要删掉它的工作副本")
	}
	if code, _, _ := c.do("GET", apiA+"/tasks", nil); code != http.StatusNotFound {
		t.Fatalf("删掉的会话应 404，实际 %d", code)
	}

	if code, _, raw := c.do("DELETE", "/api/projects/"+pid, nil); code != http.StatusOK {
		t.Fatalf("删工作区：%d %s", code, raw)
	}
	for _, dir := range []string{wsB, filepath.Join(cfg.WorkspaceRoot, "acme", pid)} {
		if _, err := os.Stat(dir); !os.IsNotExist(err) {
			t.Fatalf("删工作区要删掉基线仓库与所有会话的工作副本：%s", dir)
		}
	}
	if _, _, raw := c.do("GET", "/api/sessions", nil); strings.TrimSpace(raw) != "[]" {
		t.Fatalf("删工作区后不应再有它的会话：%s", raw)
	}
}

func TestEachTaskGetsItsOwnSandboxLevelAndModelPreference(t *testing.T) {
	ts, fake, cfg := newServer(t)
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)
	api, _ := newSession(t, c, cfg, "acme", pid)

	if code, task, raw := c.do("POST", api+"/tasks", map[string]string{"message": "讲讲这个仓库", "sandbox": "read", "model": "economy"}); code != http.StatusCreated || task["sandbox"] != "read" || task["model"] != "economy" {
		t.Fatalf("提交失败或没记下设置：%d %s", code, raw)
	}
	if code, task, raw := c.do("POST", api+"/tasks", map[string]string{"message": "修好测试"}); code != http.StatusCreated || task["sandbox"] != "write_reversible" || task["model"] != "balanced" {
		t.Fatalf("不选时应是工作区可写、均衡：%d %s", code, raw)
	}
	if len(fake.seenPolicies) != 2 || !strings.Contains(fake.seenPolicies[0], `"effect_ceiling": "read"`) || !strings.Contains(fake.seenPolicies[1], `"effect_ceiling": "write_reversible"`) {
		t.Fatalf("提交那一刻租户策略应是这个任务选的等级：%q", fake.seenPolicies)
	}
	constraints := fake.submits[0]["inputs"].(map[string]any)["constraints"].(map[string]any)
	if constraints["model_preference"] != "economy" {
		t.Fatalf("模型倾向应放进 inputs.constraints：%v", fake.submits[0])
	}
	for _, bad := range []map[string]string{{"message": "x", "sandbox": "danger_full_access"}, {"message": "x", "model": "gpt-9"}} {
		if code, _, _ := c.do("POST", api+"/tasks", bad); code != http.StatusBadRequest {
			t.Fatalf("%v 应 400，实际 %d", bad, code)
		}
	}
}

func TestFinishedTasksCacheTheirCost(t *testing.T) {
	ts, fake, cfg := newServer(t)
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)
	api, _ := newSession(t, c, cfg, "acme", pid)
	c.do("POST", api+"/tasks", map[string]string{"message": "修好测试"})

	_, _, raw := c.do("GET", api+"/tasks", nil)
	var tasks []TaskRecord
	if err := json.Unmarshal([]byte(raw), &tasks); err != nil || len(tasks) != 1 {
		t.Fatalf("任务列表：%v %s", err, raw)
	}
	if got := tasks[0]; got.Status != "TASK_STATUS_DONE" || got.CostUSD != 0.3 || got.Rounds != 2 || !got.Settled {
		t.Fatalf("结束的任务应记下花费（各 Round 之和）与轮数：%+v", got)
	}
	before := fake.queries
	c.do("GET", api+"/tasks", nil)
	if fake.queries != before {
		t.Fatal("取过计量的已结束任务不应再查 Runtime")
	}
}

func TestEnvAndActiveTasks(t *testing.T) {
	ts, fake, cfg, _ := newServerWith(t, func(c *Config) { c.Sandbox, c.SandboxImage = "docker", "python:3.13-slim" })
	fake.status = "TASK_STATUS_RUNNING"
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)
	api, ws := newSession(t, c, cfg, "acme", pid)

	code, env, raw := c.do("GET", api+"/env", nil)
	if code != http.StatusOK {
		t.Fatalf("env：%d %s", code, raw)
	}
	runtime, _ := env["runtime"].(map[string]any)
	sandbox, _ := env["sandbox"].(map[string]any)
	git, _ := env["git"].(map[string]any)
	session, _ := env["session"].(map[string]any)
	if runtime["ok"] != true || sandbox["mode"] != "docker" || sandbox["network"] != "none" || git["head"] == "" || env["effect_ceiling"] != "write_reversible" ||
		session["workspace_id"] != filepath.Base(ws) || session["own_copy"] != true {
		t.Fatalf("环境信息不对：%s", raw)
	}

	c.do("POST", api+"/tasks", map[string]string{"message": "修好测试"})
	_, _, raw = c.do("GET", "/api/tasks/active", nil)
	var active []activeTask
	if err := json.Unmarshal([]byte(raw), &active); err != nil || len(active) != 1 || active[0].ProjectID != pid || active[0].ProjectName != "Demo" || active[0].SessionID == "" || active[0].SessionTitle != "试一试" {
		t.Fatalf("进行中的任务应带上工作区与会话：%v %s", err, raw)
	}
	other := newClient(t, ts.URL)
	other.do("POST", "/api/login", map[string]string{"tenant": "globex", "user": "bob"})
	if _, _, raw := other.do("GET", "/api/tasks/active", nil); strings.TrimSpace(raw) != "[]" {
		t.Fatalf("别的租户看不到这个任务：%s", raw)
	}
}

func TestMutationsNeedTheCSRFHeader(t *testing.T) {
	ts, _, _ := newServer(t)
	c := newClient(t, ts.URL)
	c.do("POST", "/api/login", map[string]string{"tenant": "acme", "user": "alice"})
	req, _ := http.NewRequest("POST", ts.URL+"/api/projects", strings.NewReader(`{"name":"x"}`))
	res, err := c.http.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != http.StatusForbidden {
		t.Fatalf("没有 X-Coding-Web 头的改写请求应 403，实际 %d", res.StatusCode)
	}
}

func TestSafePathStaysInsideTheWorkspace(t *testing.T) {
	root := t.TempDir()
	_ = os.MkdirAll(filepath.Join(root, "a"), 0o755)
	for _, bad := range []string{"../x", "a/../../x", ".git/config", "a\\b", "/../../etc/passwd"} {
		if p, err := safePath(root, bad); err == nil && !strings.HasPrefix(p, root) {
			t.Fatalf("%q 不应解析到工作区外：%s", bad, p)
		} else if err == nil && strings.Contains(bad, ".git") {
			t.Fatalf("%q 应被拒绝", bad)
		}
	}
	if p, err := safePath(root, "a"); err != nil || p != filepath.Join(root, "a") {
		t.Fatalf("正常路径应能解析：%v %s", err, p)
	}
}
