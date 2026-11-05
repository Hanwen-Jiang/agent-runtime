// Package registry 是沙箱后端的登记（AR_SANDBOX_BACKENDS_FILE）与按租户选后端的规则（D-05、D-07、D-08、D-12、D-19）。
// SBX-01 从 Worker 的 agentruntime_worker.tools.sandboxes 与 sandbox.policy 搬来，登记文件的格式不变：
//
//		{
//		  "default": "gvisor",
//		  "backends": {
//		    "gvisor": {"kind": "k8s", "isolation": "container", "runtime_class": "gvisor", "namespace": "sandboxes",
//		               "images": {"python": "…", "universal": "…"}, "agent_image": "…", "connect": "pod_ip", "workspace_claim": "workspaces"},
//		    "cloudflare": {"kind": "cloudflare", "isolation": "microvm", "bridge_url": "https://…"},
//		    "local":  {"kind": "local", "isolation": "none"}
//		  },
//		  "environments": {…}   // 按项目定制的运行环境（D-17），见 envbuild
//		}
//
//	  - k8s：libs/go-core/adapters/k8ssandbox；kubeconfig（JSON；不给时在集群内用 ServiceAccount）、max_envs_per_scope、
//	    startup_timeout_s、run_as_user、agent_port、image_pull_policy。agent 令牌的密钥取 SecretStore 平台作用域的 sandbox/token-key。
//	  - cloudflare：libs/go-core/adapters/cfsandbox；桥的令牌取 sandbox/cloudflare-bridge-token；runtimes 是桥的镜像能跑的 runtime。
//	  - local：libs/go-core/dev/localsandbox，每个环境一个 sandbox-agent 进程，不隔离，只给开发与测试。
//	  - workspace：mount（挂卷）或 archive（不挂卷，建环境后从存档恢复、销毁前存档，D-06）；没写时 Cloudflare 是 archive，其余 mount。
//	  - enabled（默认 true）为 false 的后端不接新环境，已有的照常用到回收（D-19）；default 不能指向停用的后端。
//	  - firecracker：libs/go-core/adapters/fcsandbox，宿主上的 fc-manager（D-20）；manager_url、runtimes（宿主上有 rootfs 的运行环境）、
//	    disk_mb；令牌取 sandbox/fc-manager-token。要写 workspace: archive（客户机不挂卷）；isolation 没写时是 microvm。
//	    出网经 egress_gateway_url（客户机里的转发器 http://127.0.0.1:3128）与 pkg_mirror_url（http://127.0.0.1:3129），D-22。
//
// 用哪个后端由租户策略的 sandbox.backend 决定（按租户缓存一分钟），没写时用 default；登记里没有的名字一律拒绝。
package registry

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/cfsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/fcsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/k8ssandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/kube"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localsandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// 种类、隔离强度、能力、工作区模式。
var (
	Kinds          = []string{"k8s", "cloudflare", "local", "firecracker"}
	BuildableKinds = []string{"k8s", "cloudflare", "local", "firecracker"}
	Isolations     = []string{"none", "container", "microvm"}
	Capabilities   = []string{"snapshot", "block_attach", "vsock"}
	WorkspaceModes = []string{"mount", "archive"}
)

// IsolationRank 是隔离强度的高低。
var IsolationRank = map[string]int{"none": 0, "container": 1, "microvm": 2}

// SecretStore 平台作用域里的密钥名。
const (
	TokenKeySecret    = "sandbox/token-key"
	BridgeTokenSecret = "sandbox/cloudflare-bridge-token"
	FCManagerSecret   = "sandbox/fc-manager-token"
	SecretPurpose     = "sandbox"
)

var namePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// Spec 是登记里一个后端的原始配置。
type Spec map[string]any

func (s Spec) str(key string) string {
	if v, ok := s[key].(string); ok {
		return v
	}
	return ""
}

func (s Spec) num(key string) (int, bool) {
	switch v := s[key].(type) {
	case float64:
		return int(v), true
	case int:
		return v, true
	}
	return 0, false
}

// Kind 是后端的种类。
func (s Spec) Kind() string { return s.str("kind") }

// Str 是一个字符串字段（没有时空串）。
func (s Spec) Str(key string) string { return s.str(key) }

// Options 是构造参数。
type Options struct {
	// WorkspaceRoot 是挂卷时服务看到的工作区根（本地后端的工作目录、只读的快路径都在它下面）。
	WorkspaceRoot string
	// StateDir 放本地后端的环境（每个后端一个子目录）；要在服务重启之间保留。
	StateDir string
	// AgentBinary 是本地后端起的 sandbox-agent。
	AgentBinary string
	Secrets     secretstore.Provider
	// Faults 按后端名注入错误（测试用）。
	Faults map[string]*testkit.Faults
	// Providers 按后端名替换 Provider（测试用）。
	Providers map[string]sandbox.Provider
}

// Registry 是登记的后端。后端在第一次用到时才建（读密钥要 ctx）。
type Registry struct {
	specs        map[string]Spec
	Default      string
	Environments map[string]any
	opts         Options

	mu        sync.Mutex
	providers map[string]sandbox.Provider
}

// Load 读登记文件。
func Load(path string, opts Options) (*Registry, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读不了沙箱后端的登记文件 %s：%w", path, err)
	}
	return Parse(raw, opts)
}

// Parse 解析并核对登记。
func Parse(raw []byte, opts Options) (*Registry, error) {
	var doc struct {
		Default      string          `json:"default"`
		Backends     map[string]Spec `json:"backends"`
		Environments map[string]any  `json:"environments"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, fmt.Errorf("沙箱后端的登记不是合法的 JSON：%w", err)
	}
	if len(doc.Backends) == 0 {
		return nil, errors.New("沙箱后端的登记文件里没有 backends")
	}
	if _, ok := doc.Backends[doc.Default]; !ok {
		return nil, fmt.Errorf("沙箱后端的 default %q 没有登记", doc.Default)
	}
	for name, spec := range doc.Backends {
		if !namePattern.MatchString(name) {
			return nil, fmt.Errorf("沙箱后端名 %q 格式不对", name)
		}
		if !slices.Contains(Kinds, spec.Kind()) {
			return nil, fmt.Errorf("沙箱后端 %s 的 kind 只能是 %s", name, strings.Join(Kinds, "、"))
		}
		if iso, ok := spec["isolation"]; ok {
			if s, _ := iso.(string); !slices.Contains(Isolations, s) {
				return nil, fmt.Errorf("沙箱后端 %s 的 isolation 只能是 %s", name, strings.Join(Isolations, "、"))
			}
		}
		if ws, ok := spec["workspace"]; ok {
			if s, _ := ws.(string); !slices.Contains(WorkspaceModes, s) {
				return nil, fmt.Errorf("沙箱后端 %s 的 workspace 只能是 %s", name, strings.Join(WorkspaceModes, "、"))
			}
		}
		if en, ok := spec["enabled"]; ok {
			if _, isBool := en.(bool); !isBool {
				return nil, fmt.Errorf("沙箱后端 %s 的 enabled 只能是 true 或 false", name)
			}
		}
		if caps, ok := spec["capabilities"]; ok {
			list, isList := caps.([]any)
			valid := isList
			for _, c := range list {
				s, _ := c.(string)
				valid = valid && slices.Contains(Capabilities, s)
			}
			if !valid {
				return nil, fmt.Errorf("沙箱后端 %s 的 capabilities 只能取 %s（数组）", name, strings.Join(Capabilities, "、"))
			}
		}
		if spec.Kind() == "firecracker" {
			if spec.str("workspace") != "archive" {
				return nil, fmt.Errorf("沙箱后端 %s 是 firecracker，要写 workspace: archive（客户机不挂卷，工作区经存档恢复）", name)
			}
			if spec.str("manager_url") == "" {
				return nil, fmt.Errorf("沙箱后端 %s 是 firecracker，要写 manager_url（fc-manager 的地址）", name)
			}
		}
	}
	if en, ok := doc.Backends[doc.Default]["enabled"].(bool); ok && !en {
		return nil, fmt.Errorf("沙箱后端的 default %q 在登记里停用了（enabled: false）", doc.Default)
	}
	if opts.Faults == nil {
		opts.Faults = map[string]*testkit.Faults{}
	}
	return &Registry{specs: doc.Backends, Default: doc.Default, Environments: doc.Environments, opts: opts, providers: map[string]sandbox.Provider{}}, nil
}

// Names 是登记的后端名，排好序。
func (r *Registry) Names() []string {
	out := make([]string, 0, len(r.specs))
	for n := range r.specs {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// WorkspaceRoot 是挂卷时服务看到的工作区根（空表示没有挂卷）。
func (r *Registry) WorkspaceRoot() string { return r.opts.WorkspaceRoot }

func unknownBackend(name string) error {
	return providererr.New(providererr.Invalid, "unknown_sandbox_backend", "沙箱后端 "+name+" 没有登记")
}

// Spec 是登记里这个后端的原始配置；没登记时 invalid。
func (r *Registry) Spec(name string) (Spec, error) {
	s, ok := r.specs[name]
	if !ok {
		return nil, unknownBackend(name)
	}
	return s, nil
}

func (r *Registry) spec(name string) Spec {
	if s, ok := r.specs[name]; ok {
		return s
	}
	return Spec{}
}

// Has 判断后端是否登记了。
func (r *Registry) Has(name string) bool { _, ok := r.specs[name]; return ok }

// Enabled 判断后端接不接新环境（D-19）。
func (r *Registry) Enabled(name string) bool {
	if en, ok := r.spec(name)["enabled"].(bool); ok {
		return en
	}
	return true
}

// HasCapability 判断登记里给这个后端写了某项能力（D-19：snapshot、block_attach、vsock）；没写时都没有。
func (r *Registry) HasCapability(name, capability string) bool {
	list, _ := r.spec(name)["capabilities"].([]any)
	for _, v := range list {
		if v == capability {
			return true
		}
	}
	return false
}

// Buildable 判断后端的种类有没有实现。
func (r *Registry) Buildable(name string) bool {
	return slices.Contains(BuildableKinds, r.spec(name).Kind())
}

// Kind 是后端的种类。
func (r *Registry) Kind(name string) string { return r.spec(name).Kind() }

// Isolation 是后端的隔离强度（没写时 container）。
func (r *Registry) Isolation(name string) string {
	if s := r.spec(name).str("isolation"); s != "" {
		return s
	}
	if r.spec(name).Kind() == "firecracker" {
		return "microvm"
	}
	return "container"
}

// WorkspaceMode 是 mount 或 archive；没写时 Cloudflare 是 archive，其余 mount。
func (r *Registry) WorkspaceMode(name string) string {
	s := r.spec(name)
	if m := s.str("workspace"); m != "" {
		return m
	}
	if s.Kind() == "cloudflare" || s.Kind() == "firecracker" {
		return "archive"
	}
	return "mount"
}

// Archive 判断后端是否存档模式。
func (r *Registry) Archive(name string) bool { return r.WorkspaceMode(name) == "archive" }

// HasArchiveBackends 判断有没有存档模式的后端。
func (r *Registry) HasArchiveBackends() bool {
	for n := range r.specs {
		if r.Archive(n) {
			return true
		}
	}
	return false
}

// ImageFor 是这个后端给这个 runtime 配的镜像（images，或只给了 image 时的 python）；没配时为空。
func (r *Registry) ImageFor(name, runtime string) string {
	s := r.spec(name)
	if images, ok := s["images"].(map[string]any); ok {
		if v, ok := images[runtime].(string); ok {
			return v
		}
	}
	if runtime == "python" {
		return s.str("image")
	}
	return ""
}

// WorkspaceGroup：能用同一个工作区的后端算一组。存档模式的都是一组；挂卷的按卷分：同一个 PVC 的 K8s 后端、本地实现；
// 没登记存档模式的托管后端各自一组（挂不了工作区）。
func (r *Registry) WorkspaceGroup(name string) string {
	s := r.spec(name)
	switch {
	case r.Archive(name):
		return "archive"
	case s.Kind() == "k8s":
		return "pvc:" + s.str("workspace_claim")
	case s.Kind() == "local":
		return "local"
	default:
		return "none:" + name
	}
}

// Prices 是登记的价目（每小时、每 CPU 小时）。
func (r *Registry) Prices(name string) (perHour, perCPUHour float64) {
	price, _ := r.spec(name)["price"].(map[string]any)
	perHour, _ = price["per_hour"].(float64)
	perCPUHour, _ = price["per_cpu_hour"].(float64)
	return perHour, perCPUHour
}

// Provider 是这个后端的 Provider（第一次用到时建）。
func (r *Registry) Provider(ctx context.Context, name string) (sandbox.Provider, error) {
	s, ok := r.specs[name]
	if !ok {
		return nil, unknownBackend(name)
	}
	if !r.Buildable(name) {
		return nil, providererr.New(providererr.Invalid, "sandbox_backend_unimplemented", "沙箱后端 "+name+" 的种类 "+s.Kind()+" 还没有实现")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if p, ok := r.providers[name]; ok {
		return p, nil
	}
	if p, ok := r.opts.Providers[name]; ok {
		r.providers[name] = p
		return p, nil
	}
	p, err := r.build(ctx, name, s)
	if err != nil {
		return nil, err
	}
	r.providers[name] = p
	return p, nil
}

func (r *Registry) build(ctx context.Context, name string, s Spec) (sandbox.Provider, error) {
	faults := r.opts.Faults[name]
	switch s.Kind() {
	case "local":
		if r.opts.AgentBinary == "" {
			return nil, providererr.New(providererr.Invalid, "missing_agent_binary", "本地后端要 sandbox-agent（AR_SANDBOX_AGENT_BIN）")
		}
		state := r.opts.StateDir
		if state == "" {
			state = filepath.Join(os.TempDir(), "agentruntime-sandboxes")
		}
		return localsandbox.New(localsandbox.Options{Root: filepath.Join(state, name), Workspaces: r.opts.WorkspaceRoot, AgentBinary: r.opts.AgentBinary, Faults: faults})
	case "k8s":
		api, err := kube.Load(s.str("kubeconfig"))
		if err != nil {
			return nil, providererr.Wrap(providererr.Invalid, "kube_config", err.Error(), err)
		}
		cfg := k8ssandbox.Config{
			Namespace: s.str("namespace"), AgentImage: s.str("agent_image"), RuntimeClass: s.str("runtime_class"), Connect: s.str("connect"),
			WorkspaceClaim: s.str("workspace_claim"), ImagePullPolicy: s.str("image_pull_policy"), Images: map[string]string{},
		}
		if v, ok := s.num("agent_port"); ok {
			cfg.AgentPort = v
		}
		if v, ok := s.num("run_as_user"); ok {
			cfg.RunAsUser = v
		}
		if v, ok := s.num("max_envs_per_scope"); ok {
			cfg.MaxEnvsPerScope = v
		}
		if v, ok := s.num("startup_timeout_s"); ok {
			cfg.StartupTimeout = time.Duration(v) * time.Second
		}
		if images, ok := s["images"].(map[string]any); ok {
			for k, v := range images {
				if str, ok := v.(string); ok {
					cfg.Images[k] = str
				}
			}
		}
		if _, ok := cfg.Images["python"]; !ok {
			cfg.Images["python"] = s.str("image")
			if cfg.Images["python"] == "" {
				cfg.Images["python"] = "python:3.13-slim"
			}
		}
		if reg, ok := r.Environments["registry"].(map[string]any); ok {
			prefix, _ := reg["prefix"].(string)
			if prefix == "" {
				prefix = "envs"
			}
			pull, _ := reg["pull_host"].(string)
			cfg.CustomImagePrefix = strings.TrimRight(pull+"/"+prefix, "/")
		}
		key, err := r.ReadSecret(ctx, TokenKeySecret)
		if err != nil {
			return nil, err
		}
		return k8ssandbox.New(api, cfg, key, faults)
	case "cloudflare":
		token, err := r.ReadSecret(ctx, BridgeTokenSecret)
		if err != nil {
			return nil, err
		}
		if len(token) == 0 {
			return nil, providererr.New(providererr.Invalid, "missing_bridge_token", "SecretStore 的平台作用域里没有 "+BridgeTokenSecret+"（Cloudflare 后端 "+name+" 要用）")
		}
		var runtimes []string
		if list, ok := s["runtimes"].([]any); ok {
			for _, v := range list {
				if str, ok := v.(string); ok {
					runtimes = append(runtimes, str)
				}
			}
		}
		p, err := cfsandbox.New(s.str("bridge_url"), string(token), runtimes, faults)
		if err != nil {
			return nil, providererr.Wrap(providererr.Invalid, "invalid_bridge", err.Error(), err)
		}
		return p, nil
	case "firecracker":
		token, err := r.ReadSecret(ctx, FCManagerSecret)
		if err != nil {
			return nil, err
		}
		if len(token) == 0 {
			return nil, providererr.New(providererr.Invalid, "missing_fc_manager_token", "SecretStore 的平台作用域里没有 "+FCManagerSecret+"（Firecracker 后端 "+name+" 要用）")
		}
		cfg := fcsandbox.Config{ManagerURL: s.str("manager_url")}
		if list, ok := s["runtimes"].([]any); ok {
			for _, v := range list {
				if str, ok := v.(string); ok {
					cfg.Runtimes = append(cfg.Runtimes, str)
				}
			}
		}
		if v, ok := s.num("disk_mb"); ok {
			cfg.DiskMB = v
		}
		p, err := fcsandbox.New(cfg, strings.TrimSpace(string(token)), faults)
		if err != nil {
			return nil, providererr.Wrap(providererr.Invalid, "invalid_fc_manager", err.Error(), err)
		}
		return p, nil
	}
	return nil, unknownBackend(name)
}

// ReadSecret 读 SecretStore 平台作用域（sandbox）里的一个密钥；没有时 nil。
func (r *Registry) ReadSecret(ctx context.Context, name string) ([]byte, error) {
	if r.opts.Secrets == nil {
		return nil, nil
	}
	store, err := r.opts.Secrets.BindPlatform(SecretPurpose)
	if err != nil {
		return nil, err
	}
	v, err := store.Read(ctx, name)
	if err != nil {
		if providererr.CategoryOf(err) == providererr.NotFound {
			return nil, nil
		}
		return nil, err
	}
	return v.Reveal(), nil
}

// ReadTenantSecret 读租户自己的 SecretStore 里的一个密钥（测试用密钥 sandbox/test-env，D-16）；没有时 nil。
func (r *Registry) ReadTenantSecret(ctx context.Context, tenantID, name string) ([]byte, error) {
	if r.opts.Secrets == nil {
		return nil, nil
	}
	store, err := r.opts.Secrets.Bind(&commonv1.TenantContext{TenantId: tenantID})
	if err != nil {
		return nil, err
	}
	v, err := store.Read(ctx, name)
	if err != nil {
		if providererr.CategoryOf(err) == providererr.NotFound {
			return nil, nil
		}
		return nil, err
	}
	return v.Reveal(), nil
}

// Close 收尾持有连接的 Provider。
func (r *Registry) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, p := range r.providers {
		if c, ok := p.(sandbox.Closer); ok {
			_ = c.Close()
		}
	}
	return nil
}

// KillLocalEnvironments 杀掉 stateDir 下本地后端的全部 agent 进程（测试与 dev_stack down 用）。
func KillLocalEnvironments(stateDir string) error {
	entries, err := os.ReadDir(stateDir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	for _, e := range entries {
		if e.IsDir() {
			_ = localsandbox.KillAll(filepath.Join(stateDir, e.Name()))
		}
	}
	return nil
}
