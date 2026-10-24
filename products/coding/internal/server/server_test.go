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
	"strings"
	"testing"
)

// fakeTaskAPI 记下收到的 SubmitTask，并对 QueryTask 一律回 DONE。
type fakeTaskAPI struct {
	submits []map[string]any
	tokens  []string
}

func (f *fakeTaskAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.tokens = append(f.tokens, r.Header.Get("Authorization"))
	var body map[string]any
	_ = json.NewDecoder(r.Body).Decode(&body)
	w.Header().Set("Content-Type", "application/json")
	switch {
	case strings.HasSuffix(r.URL.Path, "/SubmitTask"):
		f.submits = append(f.submits, body)
		_ = json.NewEncoder(w).Encode(map[string]any{"taskId": "tsk_1", "sessionId": "ses_1", "eventsUrl": "http://x/v1/tasks/tsk_1/events"})
	case strings.HasSuffix(r.URL.Path, "/QueryTask"):
		_ = json.NewEncoder(w).Encode(map[string]any{"taskId": body["taskId"], "status": "TASK_STATUS_DONE"})
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
	srv, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return ts, fake, cfg
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
