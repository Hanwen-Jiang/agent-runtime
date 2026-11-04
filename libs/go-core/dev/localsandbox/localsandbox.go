// Package localsandbox 是本地 Sandbox（开发实现，仅限开发环境）。语义见 providers/sandbox。
//
// 每个环境起一个 sandbox-agent 进程（与 K8s、Cloudflare 的环境里跑的是同一个 agent，命令、文件、存档、清单、列目录与搜索的
// 规则因此完全一样），监听 127.0.0.1 的一个端口，令牌每个环境一个。环境的元数据写在 <Root>/<作用域>/<env_id>/meta.json，
// agent 进程不随 Sandbox Service 退出：服务被杀、重启之后照常找得到、用得上（SBX-01 的故障注入验收）。
//
// 工作目录：Spec.WorkspaceID 非空时是 <Workspaces>/<tenant_id>/<workspace_id>（挂卷；给了 Workspaces 才支持），否则是
// <Root>/<作用域>/<env_id>/work。不提供网络隔离与资源上限，命令以服务的用户身份在本机跑，不能运行不可信代码。
// argv[0] 为 python 时换成本机的 python3（或 Options.Python）。
package localsandbox

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/sandboxagent"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Options 是构造参数。
type Options struct {
	// Root 放每个环境的元数据与（不挂卷时的）工作目录；要在服务重启之间保留。
	Root string
	// Workspaces 是工作区根；空时不能绑定工作区（invalid，unsupported_workspace）。
	Workspaces string
	// AgentBinary 是 sandbox-agent 的可执行文件。
	AgentBinary string
	// Python 是 argv[0] 为 python 时用的解释器；空时在 PATH 上找 python3、python。
	Python string
	Faults *testkit.Faults
}

// Provider 是本地 Sandbox 的工厂。
type Provider struct {
	opts   Options
	client sandboxagent.Client
	mu     sync.Mutex
	cache  map[string]*meta
}

type meta struct {
	EnvID       string    `json:"env_id"`
	Owner       string    `json:"owner"`
	CreatedAt   time.Time `json:"created_at"`
	Workdir     string    `json:"workdir"`
	OwnsWorkdir bool      `json:"owns_workdir"`
	Port        int       `json:"port"`
	Token       string    `json:"token"`
	PID         int       `json:"pid"`
	Runtime     string    `json:"runtime"`
	WorkspaceID string    `json:"workspace_id,omitempty"`
}

// New 构造本地 Sandbox。
func New(opts Options) (*Provider, error) {
	if opts.Root == "" || opts.AgentBinary == "" {
		return nil, errors.New("localsandbox: Root 与 AgentBinary 必填")
	}
	if err := os.MkdirAll(opts.Root, 0o700); err != nil {
		return nil, err
	}
	if opts.Faults == nil {
		opts.Faults = testkit.NewFaults()
	}
	if opts.Python == "" {
		for _, name := range []string{"python3", "python"} {
			if p, err := exec.LookPath(name); err == nil {
				opts.Python = p
				break
			}
		}
	}
	return &Provider{opts: opts, client: sandboxagent.Client{}, cache: map[string]*meta{}}, nil
}

// Bind 绑定租户作用域。
func (p *Provider) Bind(t *commonv1.TenantContext) (sandbox.Sandbox, error) {
	scope, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &box{p: p, owner: scope.TenantID, tenant: true}, nil
}

// BindPlatform 绑定平台作用域。
func (p *Provider) BindPlatform(purpose string) (sandbox.Sandbox, error) {
	if _, err := tenancy.PlatformScope(purpose); err != nil {
		return nil, err
	}
	return &box{p: p, owner: devutil.PlatformID}, nil
}

// ListEnvironments 列出 Root 下还没销毁的环境（agent 进程已经没了的不算）。
func (p *Provider) ListEnvironments(_ context.Context) ([]sandbox.ListedEnvironment, error) {
	owners, err := os.ReadDir(p.opts.Root)
	if err != nil {
		return nil, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "读不了本地沙箱的目录", err)
	}
	var out []sandbox.ListedEnvironment
	for _, o := range owners {
		if !o.IsDir() {
			continue
		}
		envs, _ := os.ReadDir(filepath.Join(p.opts.Root, o.Name()))
		for _, e := range envs {
			m, err := p.load(o.Name(), e.Name())
			if err != nil || !alive(m.PID) {
				continue
			}
			out = append(out, sandbox.ListedEnvironment{ID: m.EnvID, Owner: m.Owner, CreatedAt: m.CreatedAt, State: "running"})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out, nil
}

// KillAll 杀掉 root 下所有环境的 agent 进程（测试与 dev_stack down 用）。
func KillAll(root string) error {
	owners, err := os.ReadDir(root)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	for _, o := range owners {
		envs, _ := os.ReadDir(filepath.Join(root, o.Name()))
		for _, e := range envs {
			raw, err := os.ReadFile(filepath.Join(root, o.Name(), e.Name(), "meta.json"))
			if err != nil {
				continue
			}
			var m meta
			if json.Unmarshal(raw, &m) == nil {
				kill(m.PID)
			}
		}
	}
	return nil
}

func (p *Provider) envDir(owner, envID string) string {
	return filepath.Join(p.opts.Root, owner, envID)
}

func (p *Provider) load(owner, envID string) (*meta, error) {
	p.mu.Lock()
	if m, ok := p.cache[envID]; ok && m.Owner == owner {
		p.mu.Unlock()
		return m, nil
	}
	p.mu.Unlock()
	if !validEnvID(envID) {
		return nil, notFound()
	}
	dir := p.envDir(owner, envID)
	if _, err := os.Stat(filepath.Join(dir, "destroyed")); err == nil {
		return nil, notFound()
	}
	raw, err := os.ReadFile(filepath.Join(dir, "meta.json"))
	if err != nil {
		return nil, notFound()
	}
	var m meta
	if err := json.Unmarshal(raw, &m); err != nil || m.Owner != owner || m.EnvID != envID {
		return nil, notFound()
	}
	p.mu.Lock()
	p.cache[envID] = &m
	p.mu.Unlock()
	return &m, nil
}

func notFound() error { return providererr.New(providererr.NotFound, "unknown_env", "环境不存在") }

func validEnvID(id string) bool {
	if len(id) != 20 || id[:4] != "env-" {
		return false
	}
	_, err := hex.DecodeString(id[4:])
	return err == nil
}

type box struct {
	p      *Provider
	owner  string
	tenant bool
}

func (b *box) endpoint(m *meta) sandboxagent.Endpoint {
	return sandboxagent.Endpoint{BaseURL: fmt.Sprintf("http://127.0.0.1:%d", m.Port), Token: m.Token}
}

func (b *box) get(envID string) (*meta, error) {
	m, err := b.p.load(b.owner, envID)
	if err != nil {
		return nil, err
	}
	if !alive(m.PID) {
		return nil, providererr.New(providererr.NotFound, "unknown_env", "环境不存在（agent 进程已经退出）")
	}
	return m, nil
}

func (b *box) Create(ctx context.Context, spec sandbox.Spec) (sandbox.Env, error) {
	if err := b.p.opts.Faults.Check("create"); err != nil {
		return sandbox.Env{}, err
	}
	runtime := spec.Runtime
	if runtime == "" {
		runtime = "python"
	}
	if runtime != "python" {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_runtime", "开发实现只支持 python")
	}
	if spec.Image != "" {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_image", "开发实现不用镜像")
	}
	envID := "env-" + randomHex(8)
	dir := b.p.envDir(b.owner, envID)
	m := &meta{EnvID: envID, Owner: b.owner, CreatedAt: time.Now().UTC(), Runtime: runtime, WorkspaceID: spec.WorkspaceID, Token: randomHex(24)}
	if spec.WorkspaceID != "" {
		if !b.tenant {
			return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_workspace", "平台作用域没有 workspace")
		}
		if err := sandbox.CheckWorkspaceID(spec.WorkspaceID); err != nil {
			return sandbox.Env{}, err
		}
		if b.p.opts.Workspaces == "" {
			return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_workspace", "这个 Sandbox 没有配置工作区根，不能绑定 workspace")
		}
		m.Workdir = filepath.Join(b.p.opts.Workspaces, b.owner, spec.WorkspaceID)
	} else {
		m.Workdir = filepath.Join(dir, "work")
		m.OwnsWorkdir = true
	}
	if err := os.MkdirAll(m.Workdir, 0o755); err != nil {
		return sandbox.Env{}, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "建不了工作目录", err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return sandbox.Env{}, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "建不了环境目录", err)
	}
	if err := b.p.startAgent(ctx, m); err != nil {
		_ = os.RemoveAll(dir)
		return sandbox.Env{}, err
	}
	raw, _ := json.Marshal(m)
	if err := os.WriteFile(filepath.Join(dir, "meta.json"), raw, 0o600); err != nil {
		kill(m.PID)
		return sandbox.Env{}, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "记不下环境", err)
	}
	b.p.mu.Lock()
	b.p.cache[envID] = m
	b.p.mu.Unlock()
	return sandbox.Env{ID: envID, Spec: spec, HealthConfirmed: true}, nil
}

func (p *Provider) startAgent(ctx context.Context, m *meta) error {
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		port, err := freePort()
		if err != nil {
			return providererr.Wrap(providererr.Transient, "sandbox_unavailable", "找不到空闲端口", err)
		}
		cmd := exec.Command(p.opts.AgentBinary)
		cmd.Dir = m.Workdir
		home := os.Getenv("HOME")
		if home == "" {
			home = "/tmp"
		}
		cmd.Env = []string{
			"AGENT_TOKEN=" + m.Token, "AGENT_ADDR=127.0.0.1:" + strconv.Itoa(port), "AGENT_WORKDIR=" + m.Workdir,
			"PATH=" + os.Getenv("PATH"), "HOME=" + home, "LANG=C.UTF-8",
		}
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		log, _ := os.OpenFile(filepath.Join(p.envDir(m.Owner, m.EnvID), "agent.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		cmd.Stdout, cmd.Stderr = log, log
		if err := cmd.Start(); err != nil {
			if log != nil {
				log.Close()
			}
			return providererr.Wrap(providererr.Transient, "sandbox_unavailable", "起不了 sandbox-agent", err)
		}
		go func() { _ = cmd.Wait(); log.Close() }()
		m.Port, m.PID = port, cmd.Process.Pid
		deadline := time.Now().Add(10 * time.Second)
		ep := sandboxagent.Endpoint{BaseURL: fmt.Sprintf("http://127.0.0.1:%d", port), Token: m.Token}
		for time.Now().Before(deadline) {
			if p.client.Healthy(ctx, ep) {
				return nil
			}
			if !alive(m.PID) {
				break
			}
			time.Sleep(20 * time.Millisecond)
		}
		kill(m.PID)
		lastErr = providererr.New(providererr.Transient, "sandbox_unavailable", "sandbox-agent 没有就绪")
	}
	return lastErr
}

func (b *box) Exec(ctx context.Context, envID string, argv []string, opts sandbox.ExecOptions) (sandbox.ExecResult, error) {
	if err := b.p.opts.Faults.Check("exec"); err != nil {
		return sandbox.ExecResult{}, err
	}
	m, err := b.get(envID)
	if err != nil {
		return sandbox.ExecResult{}, err
	}
	if len(argv) > 0 && argv[0] == m.Runtime && b.p.opts.Python != "" {
		argv = append([]string{b.p.opts.Python}, argv[1:]...)
	}
	return b.p.client.Exec(ctx, b.endpoint(m), argv, opts)
}

func (b *box) WriteFile(ctx context.Context, envID, path string, data []byte) error {
	if err := b.p.opts.Faults.Check("write_file"); err != nil {
		return err
	}
	if err := sandbox.CheckPath(path); err != nil {
		return err
	}
	m, err := b.get(envID)
	if err != nil {
		return err
	}
	return b.p.client.WriteFile(ctx, b.endpoint(m), path, data)
}

func (b *box) ReadFile(ctx context.Context, envID, path string) ([]byte, error) {
	if err := b.p.opts.Faults.Check("read_file"); err != nil {
		return nil, err
	}
	if err := sandbox.CheckPath(path); err != nil {
		return nil, err
	}
	m, err := b.get(envID)
	if err != nil {
		return nil, err
	}
	return b.p.client.ReadFile(ctx, b.endpoint(m), path)
}

func (b *box) ExportArchive(ctx context.Context, envID string, out io.Writer, maxBytes int64) (int64, error) {
	if err := b.p.opts.Faults.Check("export_archive"); err != nil {
		return 0, err
	}
	m, err := b.get(envID)
	if err != nil {
		return 0, err
	}
	return b.p.client.ExportArchive(ctx, b.endpoint(m), out, maxBytes)
}

func (b *box) ImportArchive(ctx context.Context, envID string, src io.Reader, size, maxBytes int64) error {
	if err := b.p.opts.Faults.Check("import_archive"); err != nil {
		return err
	}
	m, err := b.get(envID)
	if err != nil {
		return err
	}
	return b.p.client.ImportArchive(ctx, b.endpoint(m), src, size, maxBytes)
}

func (b *box) Manifest(ctx context.Context, envID string) ([]sandbox.FileEntry, error) {
	if err := b.p.opts.Faults.Check("manifest"); err != nil {
		return nil, err
	}
	m, err := b.get(envID)
	if err != nil {
		return nil, err
	}
	return b.p.client.Manifest(ctx, b.endpoint(m))
}

func (b *box) ListDir(ctx context.Context, envID, path string) ([]sandbox.DirEntry, error) {
	m, err := b.get(envID)
	if err != nil {
		return nil, err
	}
	return b.p.client.ListDir(ctx, b.endpoint(m), path)
}

func (b *box) Grep(ctx context.Context, envID, pattern, path string, maxMatches int) ([]sandbox.GrepMatch, bool, error) {
	m, err := b.get(envID)
	if err != nil {
		return nil, false, err
	}
	return b.p.client.Grep(ctx, b.endpoint(m), pattern, path, maxMatches)
}

// PreviewAddress：命令就在本机跑，预览地址转发到本机的这个端口（D-16）。
func (b *box) PreviewAddress(_ context.Context, envID string, port int) (string, error) {
	if _, err := b.get(envID); err != nil {
		return "", err
	}
	if port < 1 || port > 65535 {
		return "", providererr.New(providererr.Invalid, "invalid_port", "端口要在 1 到 65535 之间")
	}
	return "127.0.0.1:" + strconv.Itoa(port), nil
}

func (b *box) Healthy(ctx context.Context, envID string) bool {
	m, err := b.get(envID)
	return err == nil && b.p.client.Healthy(ctx, b.endpoint(m))
}

func (b *box) Destroy(_ context.Context, envID string) error {
	if err := b.p.opts.Faults.Check("destroy"); err != nil {
		return err
	}
	if !validEnvID(envID) {
		return notFound()
	}
	dir := b.p.envDir(b.owner, envID)
	if _, err := os.Stat(filepath.Join(dir, "destroyed")); err == nil {
		return nil // 已经销毁：重复调用不报错
	}
	m, err := b.p.load(b.owner, envID)
	if err != nil {
		return err
	}
	kill(m.PID)
	if m.OwnsWorkdir {
		_ = os.RemoveAll(m.Workdir)
	}
	_ = os.Remove(filepath.Join(dir, "meta.json"))
	_ = os.WriteFile(filepath.Join(dir, "destroyed"), nil, 0o600)
	b.p.mu.Lock()
	delete(b.p.cache, envID)
	b.p.mu.Unlock()
	return nil
}

func alive(pid int) bool {
	if pid <= 0 {
		return false
	}
	if err := syscall.Kill(pid, 0); err != nil {
		return false
	}
	// 僵尸进程也回应信号 0：看 /proc 的状态。
	raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return true
	}
	for i := len(raw) - 1; i >= 0; i-- {
		if raw[i] == ')' && i+2 < len(raw) {
			return raw[i+2] != 'Z' && raw[i+2] != 'X'
		}
	}
	return true
}

func kill(pid int) {
	if pid <= 0 {
		return
	}
	_ = syscall.Kill(-pid, syscall.SIGKILL)
	_ = syscall.Kill(pid, syscall.SIGKILL)
}

func freePort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
