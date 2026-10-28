// Package agent 是沙箱里的执行代理：替 Runtime 的 Sandbox 适配器在环境里执行命令、读写文件（设计书 4.10；
// 计划 docs/plan/cloud-deploy.md 的 D-05、D-07）。K8s 的沙箱 Pod 与 Cloudflare 的 Container 里跑的是同一个 agent，
// 适配器只认这一套 HTTP 接口：
//
//	GET  /healthz                 不要令牌，就绪探针用
//	POST /v1/exec                 {"argv": [...], "stdin": base64, "timeout_ms": n, "env": {…}} → {"exit_code", "timed_out", "stdout", "stderr", "truncated"}
//	PUT  /v1/files/<相对路径>      请求体是文件内容
//	GET  /v1/files/<相对路径>      响应体是文件内容；不存在（或是目录）时 404
//	GET、PUT /v1/archive、GET /v1/manifest、GET /v1/list/<相对路径>、POST /v1/grep
//	                              工作区的存档与恢复、清单、列目录与搜索（D-06；见 archive.go）
//
// 认证：除 /healthz 外都要带 X-Sandbox-Token 头。不用 Authorization：经 K8s API Server 的 pods/proxy 转发时它会被拿掉。
// 出错时返回 {"code", "message"}：400 是参数不对（invalid），401 是令牌不对，404 是不存在，413 是太大，500 是 agent 自己出错。
//
// 语义与 agentruntime_core.providers.sandbox 一致：路径一律相对工作目录（绝对路径、空段、. 与 ..、反斜杠、盘符都拒绝）；
// 超时不报错，返回 timed_out 与空的 exit_code，并杀掉整个进程组；非零退出码照常返回；命令只拿到 Config.Env 里的环境变量，
// 加上这次请求的 env（Runtime 给这个环境的包镜像地址、出网代理与测试用的密钥，D-15、D-16；名字要合法，AGENT_ 开头的一律拒绝）。
package agent

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// TokenHeader 是放令牌的请求头。
const TokenHeader = "X-Sandbox-Token"

// MinTokenLength 是令牌的最短长度。
const MinTokenLength = 32

// Config 是 agent 的配置。
type Config struct {
	// Workdir 是工作目录：命令在这里跑，文件路径相对它。
	Workdir string
	// Token 是请求要带的令牌。
	Token string
	// Env 是命令的环境变量（KEY=VALUE），不含 agent 自己的配置与令牌。
	Env []string
	// OutputLimit 是 stdout、stderr 各自最多保留的字节数；超出时保留末尾，truncated 为 true。
	OutputLimit int
	// FileLimit 是一个文件（与 stdin）最多多少字节。
	FileLimit int64
	// MaxTimeout 是一条命令最长能跑多久。
	MaxTimeout time.Duration
	// WaitDelay 是杀掉进程后最多再等多久收尾（输出管道被孙进程占着时不无限等）。
	WaitDelay time.Duration
}

type server struct {
	c Config
}

// New 检查配置并返回 agent 的 HTTP 处理器。
func New(c Config) (http.Handler, error) {
	if len(c.Token) < MinTokenLength {
		return nil, fmt.Errorf("令牌至少要 %d 个字符", MinTokenLength)
	}
	if c.Workdir == "" {
		return nil, errors.New("缺少工作目录")
	}
	if err := os.MkdirAll(c.Workdir, 0o755); err != nil {
		return nil, fmt.Errorf("建不了工作目录：%w", err)
	}
	if c.OutputLimit <= 0 {
		c.OutputLimit = 8 << 20
	}
	if c.FileLimit <= 0 {
		c.FileLimit = 64 << 20
	}
	if c.MaxTimeout <= 0 {
		c.MaxTimeout = time.Hour
	}
	if c.WaitDelay <= 0 {
		c.WaitDelay = 2 * time.Second
	}
	s := &server{c: c}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") })
	mux.Handle("POST /v1/exec", s.authed(s.exec))
	mux.Handle("PUT /v1/files/{path...}", s.authed(s.writeFile))
	mux.Handle("GET /v1/files/{path...}", s.authed(s.readFile))
	// 工作区的存档模式与在环境里执行的工作区类工具（D-06，archive.go）。
	mux.Handle("GET /v1/archive", s.authed(s.exportArchive))
	mux.Handle("PUT /v1/archive", s.authed(s.importArchive))
	mux.Handle("GET /v1/manifest", s.authed(s.manifest))
	mux.Handle("GET /v1/list", s.authed(s.listDir))
	mux.Handle("GET /v1/list/{path...}", s.authed(s.listDir))
	mux.Handle("POST /v1/grep", s.authed(s.grep))
	return mux, nil
}

func (s *server) authed(h http.HandlerFunc) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got := r.Header.Get(TokenHeader)
		if got == "" || subtle.ConstantTimeCompare([]byte(got), []byte(s.c.Token)) != 1 {
			fail(w, http.StatusUnauthorized, "unauthorized", "令牌不对")
			return
		}
		h(w, r)
	})
}

type apiError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func fail(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, apiError{Code: code, Message: message})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// --- 命令 ---

type execRequest struct {
	Argv      []string          `json:"argv"`
	Stdin     []byte            `json:"stdin"`
	TimeoutMS int64             `json:"timeout_ms"`
	Env       map[string]string `json:"env"`
}

// 一次请求最多带多少个环境变量、总共多少字节。
const (
	maxRequestEnv      = 64
	maxRequestEnvBytes = 64 << 10
)

var envName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// mergeEnv 把这次请求的环境变量并到配置的环境上（同名的以请求为准）；名字不合法、AGENT_ 开头、太多太大时报错。
func mergeEnv(base []string, extra map[string]string) ([]string, error) {
	if len(extra) == 0 {
		return base, nil
	}
	if len(extra) > maxRequestEnv {
		return nil, fmt.Errorf("env 最多 %d 个", maxRequestEnv)
	}
	size := 0
	for k, v := range extra {
		if !envName.MatchString(k) || strings.HasPrefix(strings.ToUpper(k), "AGENT_") || strings.ContainsRune(v, 0) {
			return nil, fmt.Errorf("env 的名字 %q 不合法", k)
		}
		size += len(k) + len(v)
	}
	if size > maxRequestEnvBytes {
		return nil, errors.New("env 太大")
	}
	out := make([]string, 0, len(base)+len(extra))
	for _, kv := range base {
		k, _, _ := strings.Cut(kv, "=")
		if _, replaced := extra[k]; !replaced {
			out = append(out, kv)
		}
	}
	keys := make([]string, 0, len(extra))
	for k := range extra {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		out = append(out, k+"="+extra[k])
	}
	return out, nil
}

// PassThrough 从 agent 自己的环境里挑出 names（空格或逗号分隔）列出的变量交给命令：镜像用 AGENT_ENV_PASSTHROUGH 声明
// 工具链要的变量（RUSTUP_HOME、GOCACHE 等，D-14）。AGENT_ 开头的不给。
func PassThrough(environ []string, names string) []string {
	wanted := map[string]bool{}
	for _, n := range strings.FieldsFunc(names, func(r rune) bool { return r == ' ' || r == ',' }) {
		if envName.MatchString(n) && !strings.HasPrefix(strings.ToUpper(n), "AGENT_") {
			wanted[n] = true
		}
	}
	var out []string
	for _, kv := range environ {
		if k, _, ok := strings.Cut(kv, "="); ok && wanted[k] {
			out = append(out, kv)
		}
	}
	sort.Strings(out)
	return out
}

type execResponse struct {
	ExitCode  *int   `json:"exit_code"`
	TimedOut  bool   `json:"timed_out"`
	Stdout    []byte `json:"stdout"`
	Stderr    []byte `json:"stderr"`
	Truncated bool   `json:"truncated"`
	// CPUMillis 是命令用掉的 CPU 时间（用户态加内核态，含它等待过的子进程），计量用（docs/plan/cloud-deploy.md D-07、D-10）。
	CPUMillis int64 `json:"cpu_ms"`
}

func (s *server) exec(w http.ResponseWriter, r *http.Request) {
	// stdin 以 base64 放在 JSON 里，长度约为原文的 4/3。
	r.Body = http.MaxBytesReader(w, r.Body, s.c.FileLimit*2+4096)
	var req execRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			fail(w, http.StatusRequestEntityTooLarge, "too_large", "stdin 太大")
			return
		}
		fail(w, http.StatusBadRequest, "invalid_request", "请求体不是合法的 JSON")
		return
	}
	timeout := time.Duration(req.TimeoutMS) * time.Millisecond
	switch {
	case len(req.Argv) == 0 || req.Argv[0] == "":
		fail(w, http.StatusBadRequest, "invalid_argument", "argv 不能为空")
		return
	case timeout <= 0 || timeout > s.c.MaxTimeout:
		fail(w, http.StatusBadRequest, "invalid_timeout", fmt.Sprintf("timeout_ms 要在 1 到 %d 之间", s.c.MaxTimeout.Milliseconds()))
		return
	case int64(len(req.Stdin)) > s.c.FileLimit:
		fail(w, http.StatusRequestEntityTooLarge, "too_large", "stdin 太大")
		return
	}
	env, err := mergeEnv(s.c.Env, req.Env)
	if err != nil {
		fail(w, http.StatusBadRequest, "invalid_env", err.Error())
		return
	}
	program, err := lookPath(req.Argv[0], env)
	if err != nil {
		fail(w, http.StatusBadRequest, "command_not_found", fmt.Sprintf("找不到 %s", req.Argv[0]))
		return
	}
	cmd := exec.Command(program, req.Argv[1:]...)
	cmd.Args[0] = req.Argv[0]
	cmd.Dir = s.c.Workdir
	cmd.Env = env
	cmd.Stdin = bytes.NewReader(req.Stdin)
	stdout, stderr := newTail(s.c.OutputLimit), newTail(s.c.OutputLimit)
	cmd.Stdout, cmd.Stderr = stdout, stderr
	cmd.WaitDelay = s.c.WaitDelay
	setProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		fail(w, http.StatusBadRequest, "command_not_found", fmt.Sprintf("启动不了 %s", req.Argv[0]))
		return
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	resp := execResponse{}
	select {
	case <-done:
		code := exitCode(cmd.ProcessState)
		resp.ExitCode = &code
	case <-timer.C:
		killGroup(cmd)
		<-done
		resp.TimedOut = true
	case <-r.Context().Done():
		// 调用方断开了：命令没人等，杀掉。
		killGroup(cmd)
		<-done
		return
	}
	resp.Stdout, resp.Stderr = stdout.Bytes(), stderr.Bytes()
	resp.Truncated = stdout.Truncated() || stderr.Truncated()
	if ps := cmd.ProcessState; ps != nil {
		resp.CPUMillis = (ps.UserTime() + ps.SystemTime()).Milliseconds()
	}
	writeJSON(w, http.StatusOK, resp)
}

// lookPath 按命令自己的 PATH 找程序（不是 agent 进程的 PATH）；带路径分隔符的照原样用。
func lookPath(name string, env []string) (string, error) {
	if strings.ContainsAny(name, `/\`) {
		return exec.LookPath(name)
	}
	path := ""
	for _, kv := range env {
		if k, v, ok := strings.Cut(kv, "="); ok && strings.EqualFold(k, "PATH") {
			path = v
		}
	}
	for _, dir := range filepath.SplitList(path) {
		if dir == "" {
			continue
		}
		if p, err := exec.LookPath(filepath.Join(dir, name)); err == nil {
			return p, nil
		}
	}
	return "", exec.ErrNotFound
}

// --- 文件 ---

var drive = regexp.MustCompile(`^[A-Za-z]:`)

// resolve 把请求里的相对路径变成工作目录里的路径；不合法时返回错误。
func (s *server) resolve(p string) (string, error) {
	if p == "" || strings.ContainsAny(p, "\\\x00") || strings.HasPrefix(p, "/") || drive.MatchString(p) {
		return "", errors.New("路径必须是工作目录内的相对路径")
	}
	for _, part := range strings.Split(p, "/") {
		if part == "" || part == "." || part == ".." {
			return "", errors.New("路径不得含空段、. 或 ..")
		}
	}
	return filepath.Join(s.c.Workdir, filepath.FromSlash(p)), nil
}

func (s *server) writeFile(w http.ResponseWriter, r *http.Request) {
	target, err := s.resolve(r.PathValue("path"))
	if err != nil {
		fail(w, http.StatusBadRequest, "invalid_path", err.Error())
		return
	}
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, s.c.FileLimit))
	if err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			fail(w, http.StatusRequestEntityTooLarge, "too_large", "文件太大")
			return
		}
		fail(w, http.StatusBadRequest, "invalid_request", "读不完请求体")
		return
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		fail(w, http.StatusBadRequest, "invalid_path", "建不了上级目录")
		return
	}
	if err := os.WriteFile(target, data, 0o644); err != nil {
		fail(w, http.StatusBadRequest, "invalid_path", "写不了这个文件")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *server) readFile(w http.ResponseWriter, r *http.Request) {
	target, err := s.resolve(r.PathValue("path"))
	if err != nil {
		fail(w, http.StatusBadRequest, "invalid_path", err.Error())
		return
	}
	f, err := os.Open(target)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			fail(w, http.StatusNotFound, "not_found", "文件不存在")
			return
		}
		fail(w, http.StatusInternalServerError, "read_failed", "读不了这个文件")
		return
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		fail(w, http.StatusNotFound, "not_found", "文件不存在")
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", fmt.Sprint(info.Size()))
	_, _ = io.Copy(w, f)
}

// --- 输出 ---

// tail 只保留最后 limit 个字节。
type tail struct {
	limit     int
	buf       []byte
	truncated bool
}

func newTail(limit int) *tail { return &tail{limit: limit} }

func (t *tail) Write(p []byte) (int, error) {
	n := len(p)
	t.buf = append(t.buf, p...)
	if over := len(t.buf) - t.limit; over > 0 {
		t.buf = append(t.buf[:0], t.buf[over:]...)
		t.truncated = true
	}
	return n, nil
}

func (t *tail) Bytes() []byte {
	if t.buf == nil {
		return []byte{}
	}
	return t.buf
}

func (t *tail) Truncated() bool { return t.truncated }
