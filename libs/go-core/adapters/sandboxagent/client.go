// Package sandboxagent 是 sandbox-agent 的客户端（agent 在 services/go-sandbox-agent，Rust 版在 services/rs-sandbox-agent，
// 接口相同）。本地、K8s、Cloudflare 三种后端的环境里跑的都是这个 agent，Sandbox Service 都经它执行命令、读写文件
// （D-05、D-07；SBX-01 从 Python 的 agentruntime_core.adapters.sandbox_agent 搬来）。
//
// 怎么连到 agent 由后端给出（Endpoint）：本地是 127.0.0.1 的端口；集群内直连 Pod 的地址；集群外经 K8s API Server 的
// pods/proxy；Cloudflare 经桥转发。agent 的令牌放 X-Sandbox-Token 头，额外的头（API Server、桥的认证）另放。
//
// 错误：400、409、413、422 是 invalid；404 是 not_found；401、403 是 denied；连不上、传输中断、5xx、429 是 transient。
// 存档的导出与导入走流式的请求，不把整个工作区读进内存。取消：ctx 取消时断开连接（agent 看到断开就杀掉命令的进程组），
// 中间隔着 API Server 或桥时断开不一定传得到，所以 Exec 另发 POST /v1/exec/cancel。
package sandboxagent

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
)

// TokenHeader 是放 agent 令牌的请求头。
const TokenHeader = "X-Sandbox-Token"

// Endpoint 是一个环境里的 agent。
type Endpoint struct {
	// BaseURL 是 agent 的根地址，不以 / 结尾。
	BaseURL string
	Token   string
	Headers map[string]string
	// Client 为 nil 时用不经代理的默认客户端。
	Client *http.Client
}

var direct = &http.Client{Transport: &http.Transport{Proxy: nil, MaxIdleConnsPerHost: 16, IdleConnTimeout: 90 * time.Second}}

func (e Endpoint) client() *http.Client {
	if e.Client != nil {
		return e.Client
	}
	return direct
}

// Client 是无状态的 agent 客户端。
type Client struct {
	// RequestTimeout 是普通请求的时限（Exec 另加命令自己的时限）；0 时 60 秒。
	RequestTimeout time.Duration
}

func (c Client) timeout() time.Duration {
	if c.RequestTimeout > 0 {
		return c.RequestTimeout
	}
	return 60 * time.Second
}

type execRequest struct {
	Argv      []string          `json:"argv"`
	Stdin     string            `json:"stdin"`
	TimeoutMS int64             `json:"timeout_ms"`
	ExecID    string            `json:"exec_id"`
	Env       map[string]string `json:"env,omitempty"`
}

type execResponse struct {
	ExitCode  *int   `json:"exit_code"`
	TimedOut  bool   `json:"timed_out"`
	Stdout    string `json:"stdout"`
	Stderr    string `json:"stderr"`
	Truncated bool   `json:"truncated"`
	CPUMS     *int64 `json:"cpu_ms"`
}

// Exec 执行一条命令。
func (c Client) Exec(ctx context.Context, ep Endpoint, argv []string, opts sandbox.ExecOptions) (sandbox.ExecResult, error) {
	if len(argv) == 0 {
		return sandbox.ExecResult{}, providererr.New(providererr.Invalid, "invalid_argument", "argv 不能为空")
	}
	if err := sandbox.CheckExecEnv(opts.Env); err != nil {
		return sandbox.ExecResult{}, err
	}
	ms := max(int64(1), opts.Timeout.Milliseconds())
	id := randomID()
	body, _ := json.Marshal(execRequest{Argv: argv, Stdin: base64.StdEncoding.EncodeToString(opts.Stdin), TimeoutMS: ms, ExecID: id, Env: opts.Env})
	status, data, err := c.call(ctx, ep, http.MethodPost, "/v1/exec", body, "application/json", opts.Timeout+c.timeout())
	if err != nil {
		if ctx.Err() != nil {
			// 调用方取消：断开连接之外再明确让 agent 停掉这条命令（经桥时断开不一定传得到）。
			cancelCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			cb, _ := json.Marshal(map[string]string{"exec_id": id})
			cs, _, cerr := c.call(cancelCtx, ep, http.MethodPost, "/v1/exec/cancel", cb, "application/json", 3*time.Second)
			if cerr != nil || cs < 200 || cs >= 300 {
				// 没确认停下（旧的 agent 没有取消接口是 404）：调用方不能当它停了。
				return sandbox.ExecResult{}, fmt.Errorf("%w（%w）", ctx.Err(), sandbox.ErrExecNotStopped)
			}
			return sandbox.ExecResult{}, ctx.Err()
		}
		return sandbox.ExecResult{}, err
	}
	if err := RaiseFor(status, data); err != nil {
		return sandbox.ExecResult{}, err
	}
	var out execResponse
	if err := json.Unmarshal(data, &out); err != nil {
		return sandbox.ExecResult{}, providererr.New(providererr.Transient, "sandbox_bad_response", "沙箱返回的结果格式不对")
	}
	stdout, err1 := base64.StdEncoding.DecodeString(out.Stdout)
	stderr, err2 := base64.StdEncoding.DecodeString(out.Stderr)
	if err1 != nil || err2 != nil {
		return sandbox.ExecResult{}, providererr.New(providererr.Transient, "sandbox_bad_response", "沙箱返回的结果格式不对")
	}
	res := sandbox.ExecResult{ExitCode: out.ExitCode, Stdout: stdout, Stderr: stderr, TimedOut: out.TimedOut, Truncated: out.Truncated, CPUTime: -1}
	if out.CPUMS != nil {
		res.CPUTime = time.Duration(*out.CPUMS) * time.Millisecond
	}
	return res, nil
}

// WriteFile 写一个文件（父目录不存在时 agent 建）。
func (c Client) WriteFile(ctx context.Context, ep Endpoint, path string, data []byte) error {
	if err := sandbox.CheckPath(path); err != nil {
		return err
	}
	status, body, err := c.call(ctx, ep, http.MethodPut, "/v1/files/"+quote(path), data, "application/octet-stream", 0)
	if err != nil {
		return err
	}
	return RaiseFor(status, body)
}

// ReadFile 读一个文件。
func (c Client) ReadFile(ctx context.Context, ep Endpoint, path string) ([]byte, error) {
	if err := sandbox.CheckPath(path); err != nil {
		return nil, err
	}
	status, body, err := c.call(ctx, ep, http.MethodGet, "/v1/files/"+quote(path), nil, "", 0)
	if err != nil {
		return nil, err
	}
	if err := RaiseFor(status, body); err != nil {
		return nil, err
	}
	return body, nil
}

// ExportArchive 把工作目录导出成 tar，边读边写进 out，返回字节数。tar 比文件总大小多出头部与对齐，上限放宽一些。
func (c Client) ExportArchive(ctx context.Context, ep Endpoint, out io.Writer, maxBytes int64) (int64, error) {
	if maxBytes <= 0 {
		return 0, providererr.New(providererr.Invalid, "invalid_argument", "max_bytes 要是正整数")
	}
	limit := maxBytes + maxBytes/8 + (1 << 20)
	resp, err := c.stream(ctx, ep, http.MethodGet, fmt.Sprintf("/v1/archive?max_bytes=%d", maxBytes), nil, 0, "")
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	n, err := io.Copy(out, io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return n, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "沙箱的传输中断了", err)
	}
	if n > limit {
		return n, providererr.New(providererr.Invalid, "too_large", "导出的工作区超过上限")
	}
	return n, nil
}

// ImportArchive 把 tar 导入空的工作目录，请求体从 src 流式读（Content-Length 是 size）。
func (c Client) ImportArchive(ctx context.Context, ep Endpoint, src io.Reader, size, maxBytes int64) error {
	if maxBytes <= 0 {
		return providererr.New(providererr.Invalid, "invalid_argument", "max_bytes 要是正整数")
	}
	resp, err := c.stream(ctx, ep, http.MethodPut, fmt.Sprintf("/v1/archive?max_bytes=%d", maxBytes), src, size, "application/x-tar")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	return nil
}

// Manifest 是工作目录的逐项清单，按路径排序。
func (c Client) Manifest(ctx context.Context, ep Endpoint) ([]sandbox.FileEntry, error) {
	status, body, err := c.call(ctx, ep, http.MethodGet, "/v1/manifest", nil, "", 10*time.Minute)
	if err != nil {
		return nil, err
	}
	if err := RaiseFor(status, body); err != nil {
		return nil, err
	}
	var out struct {
		Entries []sandbox.FileEntry `json:"entries"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, providererr.New(providererr.Transient, "sandbox_bad_response", "沙箱返回的清单格式不对")
	}
	sort.Slice(out.Entries, func(i, j int) bool { return out.Entries[i].Path < out.Entries[j].Path })
	return out.Entries, nil
}

// ListDir 列一个目录（空串是工作目录本身）。
func (c Client) ListDir(ctx context.Context, ep Endpoint, path string) ([]sandbox.DirEntry, error) {
	if err := sandbox.CheckDir(path); err != nil {
		return nil, err
	}
	suffix := ""
	if path != "" && path != "." {
		suffix = "/" + quote(path)
	}
	status, body, err := c.call(ctx, ep, http.MethodGet, "/v1/list"+suffix, nil, "", 0)
	if err != nil {
		return nil, err
	}
	if err := RaiseFor(status, body); err != nil {
		return nil, err
	}
	var out struct {
		Entries []sandbox.DirEntry `json:"entries"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, providererr.New(providererr.Transient, "sandbox_bad_response", "沙箱返回的目录格式不对")
	}
	return out.Entries, nil
}

// Grep 按行搜索。
func (c Client) Grep(ctx context.Context, ep Endpoint, pattern, path string, maxMatches int) ([]sandbox.GrepMatch, bool, error) {
	if err := sandbox.CheckDir(path); err != nil {
		return nil, false, err
	}
	if path == "." {
		path = ""
	}
	body, _ := json.Marshal(map[string]any{"pattern": pattern, "path": path, "max_matches": maxMatches})
	status, data, err := c.call(ctx, ep, http.MethodPost, "/v1/grep", body, "application/json", 5*time.Minute)
	if err != nil {
		return nil, false, err
	}
	if err := RaiseFor(status, data); err != nil {
		return nil, false, err
	}
	var out struct {
		Matches   []sandbox.GrepMatch `json:"matches"`
		Truncated bool                `json:"truncated"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, false, providererr.New(providererr.Transient, "sandbox_bad_response", "沙箱返回的搜索结果格式不对")
	}
	return out.Matches, out.Truncated, nil
}

// Healthy 探 /healthz。
func (c Client) Healthy(ctx context.Context, ep Endpoint) bool {
	status, _, err := c.call(ctx, ep, http.MethodGet, "/healthz", nil, "", 5*time.Second)
	return err == nil && status == http.StatusOK
}

func (c Client) call(ctx context.Context, ep Endpoint, method, path string, body []byte, contentType string, timeout time.Duration) (int, []byte, error) {
	if timeout <= 0 {
		timeout = c.timeout()
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, ep.BaseURL+path, reader)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Invalid, "invalid_endpoint", "沙箱的地址不对", err)
	}
	for k, v := range ep.Headers {
		req.Header.Set(k, v)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	req.Header.Set(TokenHeader, ep.Token)
	resp, err := ep.client().Do(req)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "连不上沙箱", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "沙箱的传输中断了", err)
	}
	return resp.StatusCode, data, nil
}

// stream 发一个请求体或响应体不整个放进内存的请求；状态码不是 2xx 时按 RaiseFor 报。时限十分钟（几个 GB 的工作区）。
func (c Client) stream(ctx context.Context, ep Endpoint, method, path string, body io.Reader, size int64, contentType string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, method, ep.BaseURL+path, body)
	if err != nil {
		return nil, providererr.Wrap(providererr.Invalid, "invalid_endpoint", "沙箱的地址不对", err)
	}
	if body != nil {
		req.ContentLength = size
	}
	for k, v := range ep.Headers {
		req.Header.Set(k, v)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	req.Header.Set(TokenHeader, ep.Token)
	client := *ep.client()
	client.Timeout = 10 * time.Minute
	resp, err := client.Do(req)
	if err != nil {
		return nil, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "沙箱的传输中断了", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()
		return nil, RaiseFor(resp.StatusCode, data)
	}
	return resp, nil
}

// RaiseFor 按 HTTP 状态码与 {"code", "message"} 给出统一错误模型的错误；2xx 返回 nil。桥（Cloudflare）的错误也用它。
func RaiseFor(status int, body []byte) error {
	if status >= 200 && status < 300 {
		return nil
	}
	var e struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	}
	_ = json.Unmarshal(body, &e)
	pick := func(code, message string) (string, string) {
		if e.Code != "" {
			code = e.Code
		}
		if e.Message != "" {
			message = e.Message
		}
		return code, message
	}
	switch status {
	case 400, 409, 413, 422:
		code, msg := pick("invalid", "沙箱拒绝了这个请求")
		return providererr.New(providererr.Invalid, code, msg)
	case 404:
		code, msg := pick("not_found", "不存在")
		return providererr.New(providererr.NotFound, code, msg)
	case 401, 403:
		code, msg := pick("sandbox_denied", "没有权限访问这个沙箱")
		return providererr.New(providererr.Denied, code, msg)
	default:
		code, msg := pick("sandbox_unavailable", fmt.Sprintf("沙箱暂时不可用（HTTP %d）", status))
		return providererr.New(providererr.Transient, code, msg)
	}
}

func quote(path string) string {
	parts := strings.Split(path, "/")
	for i, p := range parts {
		parts[i] = url.PathEscape(p)
	}
	return strings.Join(parts, "/")
}

func randomID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
