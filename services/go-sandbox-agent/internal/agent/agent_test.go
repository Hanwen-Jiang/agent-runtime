package agent

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

const testToken = "tok-0123456789abcdef0123456789abcdef"

// 测试二进制自己当被执行的命令：<测试二进制> agent-helper <模式> [参数…]。这样不依赖 python、sh，Windows 上也能跑。
func TestMain(m *testing.M) {
	if len(os.Args) > 2 && os.Args[1] == "agent-helper" {
		helper(os.Args[2], os.Args[3:])
		return
	}
	os.Exit(m.Run())
}

func helper(mode string, args []string) {
	switch mode {
	case "echo-upper":
		data, _ := io.ReadAll(os.Stdin)
		fmt.Print(strings.ToUpper(string(data)))
	case "print":
		fmt.Print(args[0])
	case "fail":
		fmt.Fprint(os.Stderr, args[1])
		code, _ := strconv.Atoi(args[0])
		os.Exit(code)
	case "sleep":
		d, _ := time.ParseDuration(args[0])
		time.Sleep(d)
	case "burn":
		d, _ := time.ParseDuration(args[0])
		x := 0
		for end := time.Now().Add(d); time.Now().Before(end); x++ {
		}
		fmt.Print(x > 0)
	case "env":
		v, ok := os.LookupEnv(args[0])
		if !ok {
			v = "<unset>"
		}
		fmt.Print(v)
	case "cat":
		data, err := os.ReadFile(args[0])
		if err != nil {
			os.Exit(2)
		}
		os.Stdout.Write(data)
	case "write":
		if err := os.WriteFile(args[0], []byte(args[1]), 0o644); err != nil {
			os.Exit(2)
		}
	case "beat":
		beat(args[0])
	case "tree":
		child := exec.Command(os.Args[0], "agent-helper", "beat", args[0]+".child")
		if err := child.Start(); err != nil {
			os.Exit(3)
		}
		beat(args[0])
	case "spam":
		n, _ := strconv.Atoi(args[0])
		os.Stdout.Write(bytes.Repeat([]byte("x"), n))
		fmt.Print("END")
	}
	os.Exit(0)
}

type harness struct {
	t      *testing.T
	srv    *httptest.Server
	dir    string
	helper string
}

func newHarness(t *testing.T, edit func(*Config)) *harness {
	t.Helper()
	dir := t.TempDir()
	env := []string{"PATH=" + os.Getenv("PATH"), "VISIBLE=yes"}
	if v := os.Getenv("SYSTEMROOT"); v != "" {
		env = append(env, "SYSTEMROOT="+v)
	}
	c := Config{Workdir: dir, Token: testToken, Env: env, OutputLimit: 1024, FileLimit: 4096, MaxTimeout: time.Minute}
	if edit != nil {
		edit(&c)
	}
	h, err := New(c)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	return &harness{t: t, srv: srv, dir: dir, helper: self}
}

func (h *harness) request(method, path string, body io.Reader, token string) *http.Response {
	h.t.Helper()
	req, err := http.NewRequest(method, h.srv.URL+path, body)
	if err != nil {
		h.t.Fatal(err)
	}
	if token != "" {
		req.Header.Set(TokenHeader, token)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	return res
}

func (h *harness) exec(argv []string, stdin string, timeout time.Duration) (int, execResponse, apiError) {
	h.t.Helper()
	body, _ := json.Marshal(execRequest{Argv: argv, Stdin: []byte(stdin), TimeoutMS: timeout.Milliseconds()})
	res := h.request(http.MethodPost, "/v1/exec", bytes.NewReader(body), testToken)
	defer res.Body.Close()
	var ok execResponse
	var bad apiError
	data, _ := io.ReadAll(res.Body)
	if res.StatusCode == http.StatusOK {
		if err := json.Unmarshal(data, &ok); err != nil {
			h.t.Fatalf("响应不是 JSON：%s", data)
		}
	} else {
		_ = json.Unmarshal(data, &bad)
	}
	return res.StatusCode, ok, bad
}

func (h *harness) run(mode string, args ...string) []string {
	return append([]string{h.helper, "agent-helper", mode}, args...)
}

func TestConfigNeedsALongToken(t *testing.T) {
	if _, err := New(Config{Workdir: t.TempDir(), Token: "short"}); err == nil {
		t.Fatal("短令牌应当被拒")
	}
}

func TestHealthNeedsNoTokenButEverythingElseDoes(t *testing.T) {
	h := newHarness(t, nil)
	if res := h.request(http.MethodGet, "/healthz", nil, ""); res.StatusCode != http.StatusOK {
		t.Fatalf("healthz：%d", res.StatusCode)
	}
	for _, token := range []string{"", "wrong-token-wrong-token-wrong-token"} {
		for _, r := range []struct{ method, path string }{{http.MethodPost, "/v1/exec"}, {http.MethodPut, "/v1/files/a.txt"}, {http.MethodGet, "/v1/files/a.txt"}} {
			res := h.request(r.method, r.path, strings.NewReader("{}"), token)
			res.Body.Close()
			if res.StatusCode != http.StatusUnauthorized {
				t.Fatalf("%s %s 令牌 %q：应为 401，实际 %d", r.method, r.path, token, res.StatusCode)
			}
		}
	}
}

func TestExecReturnsOutputAndExitCode(t *testing.T) {
	h := newHarness(t, nil)
	status, out, _ := h.exec(h.run("print", "42"), "", 30*time.Second)
	if status != 200 || out.ExitCode == nil || *out.ExitCode != 0 || string(out.Stdout) != "42" || out.TimedOut {
		t.Fatalf("%d %+v", status, out)
	}
	status, out, _ = h.exec(h.run("fail", "3", "bad"), "", 30*time.Second)
	if status != 200 || out.ExitCode == nil || *out.ExitCode != 3 || string(out.Stderr) != "bad" {
		t.Fatalf("非零退出码照常返回：%d %+v", status, out)
	}
}

func TestExecReportsCPUTimeForMetering(t *testing.T) {
	h := newHarness(t, nil)
	_, busy, _ := h.exec(h.run("burn", "300ms"), "", 30*time.Second)
	_, idle, _ := h.exec(h.run("sleep", "300ms"), "", 30*time.Second)
	if busy.CPUMillis < 100 || idle.CPUMillis > busy.CPUMillis {
		t.Fatalf("CPU 时间：忙 %dms，闲 %dms", busy.CPUMillis, idle.CPUMillis)
	}
}

func TestExecPassesStdin(t *testing.T) {
	h := newHarness(t, nil)
	_, out, _ := h.exec(h.run("echo-upper"), "abc", 30*time.Second)
	if string(out.Stdout) != "ABC" {
		t.Fatalf("stdin：%+v", out)
	}
}

func TestExecTimeoutIsReportedNotRaised(t *testing.T) {
	h := newHarness(t, nil)
	start := time.Now()
	status, out, _ := h.exec(h.run("sleep", "30s"), "", 500*time.Millisecond)
	if status != 200 || !out.TimedOut || out.ExitCode != nil {
		t.Fatalf("超时：%d %+v", status, out)
	}
	if elapsed := time.Since(start); elapsed > 10*time.Second {
		t.Fatalf("超时后要尽快返回，实际 %s", elapsed)
	}
}

func TestOutputKeepsTheTail(t *testing.T) {
	h := newHarness(t, nil)
	_, out, _ := h.exec(h.run("spam", "5000"), "", 30*time.Second)
	if !out.Truncated || len(out.Stdout) != 1024 || !strings.HasSuffix(string(out.Stdout), "END") {
		t.Fatalf("超出上限时保留末尾：truncated=%v len=%d", out.Truncated, len(out.Stdout))
	}
}

func TestCommandsOnlySeeTheConfiguredEnvironment(t *testing.T) {
	t.Setenv("AGENT_TOKEN", testToken)
	h := newHarness(t, nil)
	_, out, _ := h.exec(h.run("env", "AGENT_TOKEN"), "", 30*time.Second)
	if string(out.Stdout) != "<unset>" {
		t.Fatalf("命令拿到了 agent 的令牌：%q", out.Stdout)
	}
	_, out, _ = h.exec(h.run("env", "VISIBLE"), "", 30*time.Second)
	if string(out.Stdout) != "yes" {
		t.Fatalf("配置的环境变量要给命令：%q", out.Stdout)
	}
}

func TestRequestEnvIsAddedAndCannotTouchAgentVariables(t *testing.T) {
	h := newHarness(t, nil)
	run := func(name string, env map[string]string) (int, execResponse, apiError) {
		body, _ := json.Marshal(execRequest{Argv: h.run("env", name), TimeoutMS: 30000, Env: env})
		res := h.request(http.MethodPost, "/v1/exec", bytes.NewReader(body), testToken)
		defer res.Body.Close()
		var ok execResponse
		var bad apiError
		data, _ := io.ReadAll(res.Body)
		if res.StatusCode == http.StatusOK {
			_ = json.Unmarshal(data, &ok)
		} else {
			_ = json.Unmarshal(data, &bad)
		}
		return res.StatusCode, ok, bad
	}
	if _, out, _ := run("PIP_INDEX_URL", map[string]string{"PIP_INDEX_URL": "http://mirror/pypi/simple/"}); string(out.Stdout) != "http://mirror/pypi/simple/" {
		t.Fatalf("请求的 env 要给命令：%q", out.Stdout)
	}
	if _, out, _ := run("VISIBLE", map[string]string{"VISIBLE": "replaced"}); string(out.Stdout) != "replaced" {
		t.Fatalf("同名的以请求为准：%q", out.Stdout)
	}
	if _, out, _ := run("PIP_INDEX_URL", nil); string(out.Stdout) != "<unset>" {
		t.Fatalf("上一次请求的 env 不留到下一次：%q", out.Stdout)
	}
	for _, env := range []map[string]string{{"AGENT_TOKEN": "x"}, {"agent_addr": "x"}, {"BAD-NAME": "x"}, {"A": "x\x00y"}} {
		if status, _, bad := run("A", env); status != http.StatusBadRequest || bad.Code != "invalid_env" {
			t.Fatalf("%v：应为 400 invalid_env，实际 %d %+v", env, status, bad)
		}
	}
	many := map[string]string{}
	for i := 0; i <= maxRequestEnv; i++ {
		many[fmt.Sprintf("V%d", i)] = "x"
	}
	if status, _, _ := run("V1", many); status != http.StatusBadRequest {
		t.Fatalf("太多的 env 要拒绝：%d", status)
	}
}

func TestPassThroughOnlyGivesTheListedNonAgentVariables(t *testing.T) {
	environ := []string{"RUSTUP_HOME=/usr/local/rustup", "GOCACHE=/tmp/go", "SECRET=nope", "AGENT_TOKEN=nope"}
	got := PassThrough(environ, "RUSTUP_HOME, GOCACHE AGENT_TOKEN missing")
	if strings.Join(got, ";") != "GOCACHE=/tmp/go;RUSTUP_HOME=/usr/local/rustup" {
		t.Fatalf("%v", got)
	}
}

func TestFilesRoundtripAndAreVisibleToCommands(t *testing.T) {
	h := newHarness(t, nil)
	res := h.request(http.MethodPut, "/v1/files/data/input.txt", strings.NewReader("退款"), testToken)
	res.Body.Close()
	if res.StatusCode != http.StatusNoContent {
		t.Fatalf("写文件：%d", res.StatusCode)
	}
	res = h.request(http.MethodGet, "/v1/files/data/input.txt", nil, testToken)
	data, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if res.StatusCode != 200 || string(data) != "退款" {
		t.Fatalf("读文件：%d %q", res.StatusCode, data)
	}
	if _, out, _ := h.exec(h.run("cat", "data/input.txt"), "", 30*time.Second); string(out.Stdout) != "退款" {
		t.Fatalf("命令在工作目录里看得到文件：%+v", out)
	}
	h.exec(h.run("write", "out.txt", "from-command"), "", 30*time.Second)
	res = h.request(http.MethodGet, "/v1/files/out.txt", nil, testToken)
	data, _ = io.ReadAll(res.Body)
	res.Body.Close()
	if string(data) != "from-command" {
		t.Fatalf("命令写的文件能读回来：%q", data)
	}
}

// 读写都出不了工作目录。有的写法在路由这一步就被规范化（./a 变成 a，仍在工作目录里）或拒绝（重定向、404）；
// 要紧的是工作目录外的文件读不到、改不了。适配器在发请求之前还会按同样的规则先拒一遍。
func TestPathsCannotEscapeTheWorkdir(t *testing.T) {
	h := newHarness(t, nil)
	outside := filepath.Join(filepath.Dir(h.dir), "outside.txt")
	if err := os.WriteFile(outside, []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"../outside.txt", "a/../../outside.txt", "..%2Foutside.txt", "%2E%2E/outside.txt", "./a", "a//b", "a/", "C:/Windows/x", "%2Fetc%2Fpasswd"} {
		res := h.request(http.MethodPut, "/v1/files/"+p, strings.NewReader("PWNED"), testToken)
		res.Body.Close()
		res = h.request(http.MethodGet, "/v1/files/"+p, nil, testToken)
		data, _ := io.ReadAll(res.Body)
		res.Body.Close()
		if string(data) == "secret" {
			t.Fatalf("GET %q 读到了工作目录外的文件", p)
		}
	}
	if data, _ := os.ReadFile(outside); string(data) != "secret" {
		t.Fatalf("工作目录外的文件被改了：%q", data)
	}
	for _, p := range []string{"a%5Cb", "C:x", "..%2Foutside.txt"} {
		res := h.request(http.MethodPut, "/v1/files/"+p, strings.NewReader("x"), testToken)
		res.Body.Close()
		if res.StatusCode != http.StatusBadRequest {
			t.Fatalf("%s 是 400 invalid，实际 %d", p, res.StatusCode)
		}
	}
}

func TestMissingFilesAndDirectoriesAreNotFound(t *testing.T) {
	h := newHarness(t, nil)
	if err := os.Mkdir(filepath.Join(h.dir, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"missing.txt", "sub"} {
		res := h.request(http.MethodGet, "/v1/files/"+p, nil, testToken)
		res.Body.Close()
		if res.StatusCode != http.StatusNotFound {
			t.Fatalf("%s：应为 404，实际 %d", p, res.StatusCode)
		}
	}
}

func TestLimitsAndBadRequests(t *testing.T) {
	h := newHarness(t, nil)
	res := h.request(http.MethodPut, "/v1/files/big.bin", bytes.NewReader(make([]byte, 5000)), testToken)
	res.Body.Close()
	if res.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("超过文件上限：%d", res.StatusCode)
	}
	if status, _, e := h.exec([]string{"no-such-program-xyz"}, "", time.Second); status != 400 || e.Code != "command_not_found" {
		t.Fatalf("找不到程序：%d %+v", status, e)
	}
	if status, _, e := h.exec(nil, "", time.Second); status != 400 || e.Code != "invalid_argument" {
		t.Fatalf("空 argv：%d %+v", status, e)
	}
	if status, _, e := h.exec(h.run("print", "x"), "", 2*time.Minute); status != 400 || e.Code != "invalid_timeout" {
		t.Fatalf("超过最长时限：%d %+v", status, e)
	}
	res = h.request(http.MethodPost, "/v1/exec", strings.NewReader("not json"), testToken)
	res.Body.Close()
	if res.StatusCode != http.StatusBadRequest {
		t.Fatalf("不是 JSON：%d", res.StatusCode)
	}
}
