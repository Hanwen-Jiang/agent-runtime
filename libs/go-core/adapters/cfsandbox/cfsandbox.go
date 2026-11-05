// Package cfsandbox 是 Sandbox 的 Cloudflare 实现（D-07）：经桥（deploy/cloudflare/sandbox-bridge，一个 Cloudflare Worker）在
// Cloudflare Containers 里建环境，每个环境一台 microVM，容器里跑 sandbox-agent；命令、读写文件经桥转给 agent。
// SBX-01 从 Python 的 agentruntime_core.adapters.cloudflare_sandbox 搬来，桥的接口不变。
//
//   - 认证：桥的令牌（SecretStore 平台作用域的 sandbox/cloudflare-bridge-token）放 Authorization；作用域放 X-Sandbox-Owner，
//     桥在建环境时记下、之后每次核对。agent 的令牌只在桥里。
//   - 网络：none 时出网全断；allowlist 时只放行 AllowHosts（主机名，可以用 *. 开头；只有 80、443 与 DNS）。
//   - 不能挂工作区：WorkspaceID 非空时 invalid（unsupported_workspace），工作区靠存档与恢复（登记成 workspace: archive）。
package cfsandbox

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/sandboxagent"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

var (
	envIDPattern = regexp.MustCompile(`^cfs-[0-9a-f]{16}$`)
	hostPattern  = regexp.MustCompile(`^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$`)
)

// MaxHosts 是按域名放行的主机数上限。
const MaxHosts = 50

// UserAgent：Cloudflare 的 Browser Integrity Check 会拒绝没有 User-Agent 的请求。
const UserAgent = "agentruntime-sandbox/1"

// Provider 是 Cloudflare Sandbox 的工厂。
type Provider struct {
	BridgeURL string
	token     string
	runtimes  []string
	faults    *testkit.Faults
	agent     sandboxagent.Client
	client    *http.Client

	mu        sync.Mutex
	destroyed map[string]string
}

// New 构造 Cloudflare Sandbox。runtimes 是桥部署的镜像能跑哪些 runtime（空时只有 python）。
func New(bridgeURL, token string, runtimes []string, faults *testkit.Faults) (*Provider, error) {
	if !strings.HasPrefix(bridgeURL, "https://") && !strings.HasPrefix(bridgeURL, "http://127.0.0.1") && !strings.HasPrefix(bridgeURL, "http://localhost") {
		return nil, errors.New("桥的地址要是 https（本机测试除外）")
	}
	if len(token) < 32 {
		return nil, errors.New("桥的令牌至少 32 个字符")
	}
	if len(runtimes) == 0 {
		runtimes = []string{"python"}
	}
	if faults == nil {
		faults = testkit.NewFaults()
	}
	return &Provider{BridgeURL: strings.TrimRight(bridgeURL, "/"), token: token, runtimes: runtimes, faults: faults,
		client: &http.Client{Transport: &http.Transport{Proxy: http.ProxyFromEnvironment}}, destroyed: map[string]string{}}, nil
}

// Bind 绑定租户作用域。
func (p *Provider) Bind(t *commonv1.TenantContext) (sandbox.Sandbox, error) {
	scope, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &box{p: p, owner: scope.TenantID}, nil
}

// BindPlatform 绑定平台作用域。
func (p *Provider) BindPlatform(purpose string) (sandbox.Sandbox, error) {
	if _, err := tenancy.PlatformScope(purpose); err != nil {
		return nil, err
	}
	return &box{p: p, owner: devutil.PlatformID}, nil
}

func (p *Provider) headers(owner string) map[string]string {
	return map[string]string{"Authorization": "Bearer " + p.token, "X-Sandbox-Owner": owner, "User-Agent": UserAgent}
}

func (p *Provider) call(ctx context.Context, method, path, owner string, body any) (int, []byte, error) {
	var reader io.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		reader = bytes.NewReader(raw)
	}
	ctx, cancel := context.WithTimeout(ctx, 120*time.Second) // 建环境要等容器冷启动
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, p.BridgeURL+path, reader)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Invalid, "invalid_endpoint", "桥的地址不对", err)
	}
	for k, v := range p.headers(owner) {
		req.Header.Set(k, v)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := p.client.Do(req)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "连不上 Cloudflare 的沙箱桥", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "桥的传输中断了", err)
	}
	return resp.StatusCode, data, nil
}

// ListEnvironments 是桥的目录里本部署的全部环境（平台作用域），给对账用（D-11）。
func (p *Provider) ListEnvironments(ctx context.Context) ([]sandbox.ListedEnvironment, error) {
	status, body, err := p.call(ctx, http.MethodGet, "/v1/sandboxes", devutil.PlatformID, nil)
	if err != nil {
		return nil, err
	}
	if err := sandboxagent.RaiseFor(status, body); err != nil {
		return nil, err
	}
	var out struct {
		Environments []struct {
			EnvID     string `json:"env_id"`
			Owner     string `json:"owner"`
			CreatedAt string `json:"created_at"`
		} `json:"environments"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, providererr.New(providererr.Transient, "sandbox_bad_response", "桥返回的列表格式不对")
	}
	var listed []sandbox.ListedEnvironment
	for _, e := range out.Environments {
		if !envIDPattern.MatchString(e.EnvID) {
			continue
		}
		created, _ := time.Parse(time.RFC3339Nano, e.CreatedAt)
		listed = append(listed, sandbox.ListedEnvironment{ID: e.EnvID, Owner: e.Owner, CreatedAt: created, State: "running"})
	}
	return listed, nil
}

type box struct {
	p     *Provider
	owner string
}

func (b *box) endpoint(envID string) (sandboxagent.Endpoint, error) {
	b.p.mu.Lock()
	owner, gone := b.p.destroyed[envID]
	b.p.mu.Unlock()
	if !envIDPattern.MatchString(envID) || (gone && owner == b.owner) {
		return sandboxagent.Endpoint{}, providererr.New(providererr.NotFound, "unknown_env", "环境不存在")
	}
	// agent 的令牌在桥里加上；这里的 X-Sandbox-Token 留空，桥会换掉。
	return sandboxagent.Endpoint{BaseURL: b.p.BridgeURL + "/v1/sandboxes/" + envID + "/agent", Headers: b.p.headers(b.owner), Client: b.p.client}, nil
}

func (b *box) Create(ctx context.Context, spec sandbox.Spec) (sandbox.Env, error) {
	spec = spec.WithDefaults()
	if err := b.p.faults.Check("create"); err != nil {
		return sandbox.Env{}, err
	}
	runtime := spec.Runtime
	if runtime == "" {
		runtime = "python"
	}
	if !slices.Contains(b.p.runtimes, runtime) {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_runtime", "Cloudflare 后端的镜像只能跑 "+strings.Join(b.p.runtimes, "、"))
	}
	if spec.Image != "" {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_image", "Cloudflare 后端的镜像在桥里写死，不能按项目定制（D-17）")
	}
	if spec.WorkspaceID != "" {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_workspace", "Cloudflare 后端不能挂工作区，要按存档模式用（D-06）")
	}
	hosts := []string{}
	switch spec.Network {
	case "allowlist":
		for _, h := range spec.AllowHosts {
			hosts = append(hosts, strings.ToLower(h))
		}
		ok := len(hosts) > 0 && len(hosts) <= MaxHosts
		for _, h := range hosts {
			ok = ok && hostPattern.MatchString(h)
		}
		if !ok {
			return sandbox.Env{}, providererr.New(providererr.Invalid, "invalid_allow_hosts", "allow_hosts 要是 1 到 50 个主机名（可以用 *. 开头）")
		}
	case "", "none":
	default:
		return sandbox.Env{}, providererr.New(providererr.Invalid, "invalid_argument", "network 只能是 none 或 allowlist")
	}
	idle := int(spec.IdleTimeout.Seconds())
	if idle < 1 || idle > 86400 {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "invalid_argument", "idle_timeout 要在 1 秒到 1 天之间")
	}
	status, body, err := b.p.call(ctx, http.MethodPost, "/v1/sandboxes", b.owner, map[string]any{"idle_seconds": idle, "allow_hosts": hosts})
	if err != nil {
		return sandbox.Env{}, err
	}
	if err := sandboxagent.RaiseFor(status, body); err != nil {
		return sandbox.Env{}, err
	}
	var out struct {
		EnvID string `json:"env_id"`
	}
	if json.Unmarshal(body, &out) != nil || !envIDPattern.MatchString(out.EnvID) {
		return sandbox.Env{}, providererr.New(providererr.Transient, "sandbox_bad_response", "桥返回的环境 id 格式不对")
	}
	return sandbox.Env{ID: out.EnvID, Spec: spec, HealthConfirmed: b.Healthy(ctx, out.EnvID)}, nil
}

func (b *box) Healthy(ctx context.Context, envID string) bool {
	ep, err := b.endpoint(envID)
	return err == nil && b.p.agent.Healthy(ctx, ep)
}

func (b *box) Exec(ctx context.Context, envID string, argv []string, opts sandbox.ExecOptions) (sandbox.ExecResult, error) {
	if err := b.p.faults.Check("exec"); err != nil {
		return sandbox.ExecResult{}, err
	}
	ep, err := b.endpoint(envID)
	if err != nil {
		return sandbox.ExecResult{}, err
	}
	return b.p.agent.Exec(ctx, ep, argv, opts)
}

func (b *box) WriteFile(ctx context.Context, envID, path string, data []byte) error {
	if err := b.p.faults.Check("write_file"); err != nil {
		return err
	}
	ep, err := b.endpoint(envID)
	if err != nil {
		return err
	}
	return b.p.agent.WriteFile(ctx, ep, path, data)
}

func (b *box) ReadFile(ctx context.Context, envID, path string) ([]byte, error) {
	if err := b.p.faults.Check("read_file"); err != nil {
		return nil, err
	}
	ep, err := b.endpoint(envID)
	if err != nil {
		return nil, err
	}
	return b.p.agent.ReadFile(ctx, ep, path)
}

// ExportArchive 等：桥原样转给容器里的 agent（导入的存档大小受 Workers 内存上限 128 MB 限制；导出是流式的）。
func (b *box) ExportArchive(ctx context.Context, envID string, out io.Writer, maxBytes int64) (int64, error) {
	if err := b.p.faults.Check("export_archive"); err != nil {
		return 0, err
	}
	ep, err := b.endpoint(envID)
	if err != nil {
		return 0, err
	}
	return b.p.agent.ExportArchive(ctx, ep, out, maxBytes)
}

func (b *box) ImportArchive(ctx context.Context, envID string, src io.Reader, size, maxBytes int64) error {
	if err := b.p.faults.Check("import_archive"); err != nil {
		return err
	}
	ep, err := b.endpoint(envID)
	if err != nil {
		return err
	}
	return b.p.agent.ImportArchive(ctx, ep, src, size, maxBytes)
}

func (b *box) Manifest(ctx context.Context, envID string) ([]sandbox.FileEntry, error) {
	if err := b.p.faults.Check("manifest"); err != nil {
		return nil, err
	}
	ep, err := b.endpoint(envID)
	if err != nil {
		return nil, err
	}
	return b.p.agent.Manifest(ctx, ep)
}

func (b *box) ListDir(ctx context.Context, envID, path string) ([]sandbox.DirEntry, error) {
	ep, err := b.endpoint(envID)
	if err != nil {
		return nil, err
	}
	return b.p.agent.ListDir(ctx, ep, path)
}

func (b *box) Grep(ctx context.Context, envID, pattern, path string, maxMatches int) ([]sandbox.GrepMatch, bool, error) {
	ep, err := b.endpoint(envID)
	if err != nil {
		return nil, false, err
	}
	return b.p.agent.Grep(ctx, ep, pattern, path, maxMatches)
}

func (b *box) Destroy(ctx context.Context, envID string) error {
	if err := b.p.faults.Check("destroy"); err != nil {
		return err
	}
	if !envIDPattern.MatchString(envID) {
		return providererr.New(providererr.NotFound, "unknown_env", "环境不存在")
	}
	b.p.mu.Lock()
	owner, gone := b.p.destroyed[envID]
	b.p.mu.Unlock()
	if gone && owner == b.owner {
		return nil
	}
	status, body, err := b.p.call(ctx, http.MethodDelete, "/v1/sandboxes/"+envID, b.owner, nil)
	if err != nil {
		return err
	}
	if err := sandboxagent.RaiseFor(status, body); err != nil {
		return err
	}
	b.p.mu.Lock()
	b.p.destroyed[envID] = b.owner
	b.p.mu.Unlock()
	return nil
}
