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
)

// fakeTaskAPI 记下收到的 SubmitTask（与那一刻租户策略文件的内容），对 QueryTask 回 status（默认 DONE）与两个 Round 的花费。
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
		id := "tsk_" + strconv.Itoa(len(f.submits))
		_ = json.NewEncoder(w).Encode(map[string]any{"taskId": id, "sessionId": "ses_1", "eventsUrl": "http://x/v1/tasks/" + id + "/events"})
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

func TestLoginProjectChangesCommitAndTask(t *testing.T) {
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

	code, project, raw := c.do("POST", "/api/projects", map[string]string{"name": "Demo", "sample": "ci_rounding"})
	if code != http.StatusCreated {
		t.Fatalf("建项目失败：%d %s", code, raw)
	}
	pid := project["id"].(string)
	ws := filepath.Join(cfg.WorkspaceRoot, "acme", pid)
	if _, err := os.Stat(filepath.Join(ws, "pricing.py")); err != nil {
		t.Fatalf("示例文件应在工作区里：%v", err)
	}

	if err := os.WriteFile(filepath.Join(ws, "new.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, changes, _ := c.do("GET", "/api/projects/"+pid+"/changes", nil)
	if files, _ := changes["files"].([]any); len(files) != 1 || !strings.Contains(changes["diff"].(string), "+hello") {
		t.Fatalf("改动里应有新文件：%v", changes)
	}
	if code, _, raw := c.do("POST", "/api/projects/"+pid+"/commit", map[string]string{"message": "add new.txt"}); code != http.StatusOK {
		t.Fatalf("提交失败：%d %s", code, raw)
	}
	_, changes, _ = c.do("GET", "/api/projects/"+pid+"/changes", nil)
	if files, _ := changes["files"].([]any); len(files) != 0 {
		t.Fatalf("提交之后不应再有改动：%v", changes)
	}

	code, task, raw := c.do("POST", "/api/projects/"+pid+"/tasks", map[string]string{"message": "fix the tests"})
	if code != http.StatusCreated || task["id"] != "tsk_1" {
		t.Fatalf("提交任务失败：%d %s", code, raw)
	}
	if len(fake.submits) != 1 || fake.submits[0]["workspaceId"] != pid || fake.submits[0]["sessionAccess"] != "SESSION_ACCESS_READ_WRITE" {
		t.Fatalf("SubmitTask 的请求不对：%v", fake.submits)
	}
	if !strings.HasPrefix(fake.tokens[0], "Bearer ") {
		t.Fatal("要把令牌转给 Task API")
	}
	if code, _, raw := c.do("GET", "/api/projects/"+pid+"/tasks", nil); code != http.StatusOK || !strings.Contains(raw, "TASK_STATUS_DONE") {
		t.Fatalf("任务列表应按 Runtime 刷新到 DONE：%d %s", code, raw)
	}

	// 别的租户看不到这个项目。
	other := newClient(t, ts.URL)
	other.do("POST", "/api/login", map[string]string{"tenant": "globex", "user": "bob"})
	if code, _, _ := other.do("GET", "/api/projects/"+pid+"/changes", nil); code != http.StatusNotFound {
		t.Fatalf("别的租户访问应 404，实际 %d", code)
	}
}

// sampleProject 登录 acme/alice 并从示例建一个项目，返回项目 id。
func sampleProject(t *testing.T, c *client) string {
	t.Helper()
	c.do("POST", "/api/login", map[string]string{"tenant": "acme", "user": "alice"})
	code, project, raw := c.do("POST", "/api/projects", map[string]string{"name": "Demo", "sample": "ci_rounding"})
	if code != http.StatusCreated {
		t.Fatalf("建项目失败：%d %s", code, raw)
	}
	return project["id"].(string)
}

func TestEachTaskGetsItsOwnSandboxLevelAndModelPreference(t *testing.T) {
	ts, fake, _ := newServer(t)
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)

	if code, task, raw := c.do("POST", "/api/projects/"+pid+"/tasks", map[string]string{"message": "讲讲这个仓库", "sandbox": "read", "model": "economy"}); code != http.StatusCreated || task["sandbox"] != "read" || task["model"] != "economy" {
		t.Fatalf("提交失败或没记下设置：%d %s", code, raw)
	}
	if code, task, raw := c.do("POST", "/api/projects/"+pid+"/tasks", map[string]string{"message": "修好测试"}); code != http.StatusCreated || task["sandbox"] != "write_reversible" || task["model"] != "balanced" {
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
		if code, _, _ := c.do("POST", "/api/projects/"+pid+"/tasks", bad); code != http.StatusBadRequest {
			t.Fatalf("%v 应 400，实际 %d", bad, code)
		}
	}
}

func TestFinishedTasksCacheTheirCost(t *testing.T) {
	ts, fake, _ := newServer(t)
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)
	c.do("POST", "/api/projects/"+pid+"/tasks", map[string]string{"message": "修好测试"})

	_, _, raw := c.do("GET", "/api/projects/"+pid+"/tasks", nil)
	var tasks []TaskRecord
	if err := json.Unmarshal([]byte(raw), &tasks); err != nil || len(tasks) != 1 {
		t.Fatalf("任务列表：%v %s", err, raw)
	}
	if got := tasks[0]; got.Status != "TASK_STATUS_DONE" || got.CostUSD != 0.3 || got.Rounds != 2 || !got.Settled {
		t.Fatalf("结束的任务应记下花费（各 Round 之和）与轮数：%+v", got)
	}
	before := fake.queries
	c.do("GET", "/api/projects/"+pid+"/tasks", nil)
	if fake.queries != before {
		t.Fatal("取过计量的已结束任务不应再查 Runtime")
	}
}

func TestEnvAndActiveTasks(t *testing.T) {
	ts, fake, _, _ := newServerWith(t, func(c *Config) { c.Sandbox, c.SandboxImage = "docker", "python:3.13-slim" })
	fake.status = "TASK_STATUS_RUNNING"
	c := newClient(t, ts.URL)
	pid := sampleProject(t, c)

	code, env, raw := c.do("GET", "/api/projects/"+pid+"/env", nil)
	if code != http.StatusOK {
		t.Fatalf("env：%d %s", code, raw)
	}
	runtime, _ := env["runtime"].(map[string]any)
	sandbox, _ := env["sandbox"].(map[string]any)
	git, _ := env["git"].(map[string]any)
	if runtime["ok"] != true || sandbox["mode"] != "docker" || sandbox["network"] != "none" || git["head"] == "" || env["effect_ceiling"] != "write_reversible" {
		t.Fatalf("环境信息不对：%s", raw)
	}

	c.do("POST", "/api/projects/"+pid+"/tasks", map[string]string{"message": "修好测试"})
	_, _, raw = c.do("GET", "/api/tasks/active", nil)
	var active []activeTask
	if err := json.Unmarshal([]byte(raw), &active); err != nil || len(active) != 1 || active[0].ProjectID != pid || active[0].ProjectName != "Demo" {
		t.Fatalf("后台任务应列出进行中的任务与所属项目：%v %s", err, raw)
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
