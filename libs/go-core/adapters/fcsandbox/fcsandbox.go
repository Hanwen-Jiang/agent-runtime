// Package fcsandbox 是 Sandbox 的 Firecracker 实现（D-20 第二段）：经宿主上的 fc-manager（services/rs-fc-manager，systemd
// 服务）每个环境起一台 microVM，客户机里 sandbox-agent 当 PID 1（--init），只经 vsock 可达；命令、读写文件经 fc-manager
// 的 /v1/envs/<id>/agent/… 原样转给 agent。接口与 Cloudflare 桥同形，所以这里与 cfsandbox 几乎一样：
//
//   - 认证：fc-manager 的令牌（SecretStore 平台作用域的 sandbox/fc-manager-token）放 Authorization；作用域放 X-Sandbox-Owner，
//     fc-manager 建环境时记下、之后每次核对。agent 的令牌只在 fc-manager 的内存里。
//   - 网络：客户机没有网卡。只收 network = none；联网一律经出网网关（D-22：客户机里 127.0.0.1:3128 的转发器 → vsock →
//     fc-manager → 网关），代理变量由 Sandbox Service 按登记的 egress_gateway_url 注入，授权照旧在网关里。
//   - 不能挂工作区：WorkspaceID 非空时 invalid（unsupported_workspace），登记成 workspace: archive。
//   - 项目镜像只有经运维按不可变 OCI digest 转换并登记 image_runtimes 后才能使用；未登记或跨租户引用拒绝，不能冒充官方环境。
//   - 预览：fc-manager 在它的监听地址上为这个客户机端口开一个入口（POST /v1/envs/<id>/preview），出网网关的预览入口连它。
package fcsandbox

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/sandboxagent"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

var envIDPattern = regexp.MustCompile(`^fc-[0-9a-f]{16}$`)
var runtimePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
var scopedImagePattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}/env@sha256:[0-9a-f]{64}$`)
var imageDigestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

type PreparedImage struct {
	Runtime      string
	SourceDigest string
}

// Config 是登记里 firecracker 后端的参数。
type Config struct {
	// ManagerURL 是 fc-manager 的地址（http://10.42.0.1:8097：只听 k3s 的 cni0，流量不出宿主）。
	ManagerURL string
	// Runtimes 是宿主上有 rootfs 的运行环境（fc-manager 的 AR_FC_IMAGES_DIR/<runtime>.ext4）；空时只有 python。
	Runtimes []string
	// DiskMB 是每台 VM 的可写盘（scratch）大小；0 用 fc-manager 的默认值。
	DiskMB int
	// GuestProfile is operator-controlled; task specs cannot select it.
	// Empty/workspace preserves read-only root and the ordinary guest UID.
	GuestProfile string
	RootDiskMB   int
	// Benchmark-only base image containing the matching privileged guest agent.
	// Public runtime names stay unchanged; callers cannot choose this alias.
	BaseRuntime     string
	BaseImageDigest string
	// ImageRuntimes maps operator-prepared immutable OCI references to rootfs aliases.
	// An entry is usable only by the tenant named below CustomImagePrefix.
	CustomImagePrefix string
	ImageRuntimes     map[string]PreparedImage
	// ImagePrepareURL is a private, authenticated preparer for D17 OCI images.
	// When absent, custom images still require a static mapping.
	ImagePrepareURL string
}

// Provider 是 Firecracker Sandbox 的工厂。
type Provider struct {
	cfg    Config
	token  string
	faults *testkit.Faults
	agent  sandboxagent.Client
	client *http.Client
}

// ValidateGuestProfile validates only operator configuration. Root capacity is
// separately constrained by the manager's configured per-VM and host limits.
func ValidateGuestProfile(profile string, rootDiskMB int) error {
	if profile == "" {
		profile = "workspace"
	}
	if profile != "workspace" && profile != "benchmark-system" {
		return errors.New("unsupported Firecracker guest_profile")
	}
	if profile == "workspace" && rootDiskMB != 0 {
		return errors.New("root_disk_mb is forbidden for workspace guest_profile")
	}
	if profile == "benchmark-system" && (rootDiskMB < 64 || int64(rootDiskMB) > (1<<63-1)>>20) {
		return errors.New("root_disk_mb must be at least 64 MiB and fit the disk byte capacity")
	}
	return nil
}

func ValidateGuestBase(profile, runtime, digest string) error {
	if runtime == "" && digest == "" {
		return nil
	}
	if profile != "benchmark-system" {
		return errors.New("base_runtime and base_image_digest require benchmark-system guest_profile")
	}
	if !runtimePattern.MatchString(runtime) || !imageDigestPattern.MatchString(digest) {
		return errors.New("base_runtime and base_image_digest must form a valid runtime alias and immutable source digest pair")
	}
	return nil
}

// New 构造。
func New(cfg Config, token string, faults *testkit.Faults) (*Provider, error) {
	if err := ValidateGuestProfile(cfg.GuestProfile, cfg.RootDiskMB); err != nil {
		return nil, err
	}
	if err := ValidateGuestBase(cfg.GuestProfile, cfg.BaseRuntime, cfg.BaseImageDigest); err != nil {
		return nil, err
	}
	if cfg.GuestProfile == "" {
		cfg.GuestProfile = "workspace"
	}
	if err := validatePrepareURL(cfg.ImagePrepareURL, cfg.CustomImagePrefix); err != nil {
		return nil, err
	}
	for image, prepared := range cfg.ImageRuntimes {
		if cfg.CustomImagePrefix == "" || !strings.HasPrefix(image, strings.TrimRight(cfg.CustomImagePrefix, "/")+"/") || !runtimePattern.MatchString(prepared.Runtime) || !imageDigestPattern.MatchString(prepared.SourceDigest) {
			return nil, errors.New("Firecracker image_runtimes must map scoped immutable images to valid prepared runtimes")
		}
		rest := strings.TrimPrefix(image, strings.TrimRight(cfg.CustomImagePrefix, "/")+"/")
		if !scopedImagePattern.MatchString(rest) {
			return nil, errors.New("Firecracker image_runtimes requires tenant-scoped digest references")
		}
	}
	cfg.ManagerURL = strings.TrimRight(cfg.ManagerURL, "/")
	if !strings.HasPrefix(cfg.ManagerURL, "http://") && !strings.HasPrefix(cfg.ManagerURL, "https://") {
		return nil, errors.New("manager_url 要是 http:// 或 https:// 的地址")
	}
	if len(token) < 32 {
		return nil, errors.New("fc-manager 的令牌至少 32 个字符")
	}
	if len(cfg.Runtimes) == 0 {
		cfg.Runtimes = []string{"python"}
	}
	if cfg.DiskMB < 0 {
		return nil, errors.New("disk_mb 不能是负数")
	}
	if faults == nil {
		faults = testkit.NewFaults()
	}
	// fc-manager 在宿主上、不经代理。
	return &Provider{cfg: cfg, token: token, faults: faults, client: &http.Client{Transport: &http.Transport{Proxy: nil}}}, nil
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
	return map[string]string{"Authorization": "Bearer " + p.token, "X-Sandbox-Owner": owner}
}

func (p *Provider) call(ctx context.Context, method, path, owner string, body any) (int, []byte, error) {
	var reader io.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		reader = bytes.NewReader(raw)
	}
	ctx, cancel := context.WithTimeout(ctx, 90*time.Second) // 建环境要等 VM 启动
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, p.cfg.ManagerURL+path, reader)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Invalid, "invalid_endpoint", "fc-manager 的地址不对", err)
	}
	for k, v := range p.headers(owner) {
		req.Header.Set(k, v)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := p.client.Do(req)
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "连不上 fc-manager", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return 0, nil, providererr.Wrap(providererr.Transient, "sandbox_unavailable", "fc-manager 的传输中断了", err)
	}
	return resp.StatusCode, data, nil
}

// ListEnvironments 是 fc-manager 上在跑的全部环境（平台作用域），给对账用（D-11）。
func (p *Provider) ListEnvironments(ctx context.Context) ([]sandbox.ListedEnvironment, error) {
	status, body, err := p.call(ctx, http.MethodGet, "/v1/envs", devutil.PlatformID, nil)
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
			State     string `json:"state"`
		} `json:"environments"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, providererr.New(providererr.Transient, "sandbox_bad_response", "fc-manager 返回的列表格式不对")
	}
	var listed []sandbox.ListedEnvironment
	for _, e := range out.Environments {
		if !envIDPattern.MatchString(e.EnvID) {
			continue
		}
		created, _ := time.Parse(time.RFC3339Nano, e.CreatedAt)
		state := e.State
		if state == "" {
			state = "running"
		}
		listed = append(listed, sandbox.ListedEnvironment{ID: e.EnvID, Owner: e.Owner, CreatedAt: created, State: state})
	}
	return listed, nil
}

type box struct {
	p     *Provider
	owner string
}

func (b *box) rejectCreatedProfile(ctx context.Context, envID string) error {
	if err := b.Destroy(context.WithoutCancel(ctx), envID); err != nil {
		return providererr.New(providererr.Transient, "guest_profile_mismatch", "Firecracker guest profile differs from operator configuration; VM "+envID+" cleanup requires retry")
	}
	return providererr.New(providererr.Invalid, "guest_profile_mismatch", "Firecracker guest profile differs from operator configuration; VM destroyed")
}

func (b *box) endpoint(envID string) (sandboxagent.Endpoint, error) {
	if !envIDPattern.MatchString(envID) {
		return sandboxagent.Endpoint{}, providererr.New(providererr.NotFound, "unknown_env", "环境不存在")
	}
	// agent 的令牌由 fc-manager 换上；这里的 X-Sandbox-Token 留空。
	return sandboxagent.Endpoint{BaseURL: b.p.cfg.ManagerURL + "/v1/envs/" + envID + "/agent", Headers: b.p.headers(b.owner), Client: b.p.client}, nil
}

func (b *box) Create(ctx context.Context, spec sandbox.Spec) (sandbox.Env, error) {
	spec = spec.WithDefaults()
	if err := b.p.faults.Check("create"); err != nil {
		return sandbox.Env{}, err
	}
	runtime := spec.Runtime
	supported := false
	for _, r := range b.p.cfg.Runtimes {
		supported = supported || r == runtime
	}
	if !supported {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_runtime", "Firecracker 后端只有这些运行环境："+strings.Join(b.p.cfg.Runtimes, "、"))
	}
	var prepared PreparedImage
	if spec.Image != "" {
		var err error
		prepared, err = b.preparedImage(ctx, spec.Image)
		if err != nil {
			return sandbox.Env{}, err
		}
		runtime = prepared.Runtime
	} else if b.p.cfg.GuestProfile == "benchmark-system" {
		if b.p.cfg.BaseRuntime == "" {
			return sandbox.Env{}, providererr.New(providererr.Invalid, "profile_base_unavailable", "benchmark guest requires an operator-prepared base image for this runtime")
		}
		prepared = PreparedImage{Runtime: b.p.cfg.BaseRuntime, SourceDigest: b.p.cfg.BaseImageDigest}
		runtime = prepared.Runtime
	}
	if spec.WorkspaceID != "" {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_workspace", "Firecracker 后端不能挂工作区，要按存档模式用（D-06）")
	}
	if spec.Network != "none" {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "network_allowlist_unsupported", "Firecracker 的客户机没有网卡，联网只经出网网关的授权")
	}
	req := map[string]any{"runtime": runtime, "cpu_millis": spec.CPUMillis, "memory_mb": spec.MemoryMB}
	if b.p.cfg.GuestProfile != "workspace" {
		req["guest_profile"] = b.p.cfg.GuestProfile
		req["root_disk_mb"] = b.p.cfg.RootDiskMB
	}
	if b.p.cfg.DiskMB > 0 {
		req["disk_mb"] = b.p.cfg.DiskMB
	}
	if spec.SessionID != "" && len(spec.SessionID) <= 256 {
		req["labels"] = map[string]string{"session": spec.SessionID}
	}
	status, body, err := b.p.call(ctx, http.MethodPost, "/v1/envs", b.owner, req)
	if err != nil {
		return sandbox.Env{}, err
	}
	if err := sandboxagent.RaiseFor(status, body); err != nil {
		return sandbox.Env{}, err
	}
	var out struct {
		EnvID        string `json:"env_id"`
		Image        string `json:"image"`
		GuestProfile string `json:"guest_profile"`
		RootDiskMB   int    `json:"root_disk_mb"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		// A profile field with a wrong JSON type is also a failed identity
		// confirmation. Retain enough of the response to clean up its VM.
		var identity struct {
			EnvID string `json:"env_id"`
		}
		if json.Unmarshal(body, &identity) == nil && envIDPattern.MatchString(identity.EnvID) {
			return sandbox.Env{}, b.rejectCreatedProfile(ctx, identity.EnvID)
		}
		return sandbox.Env{}, providererr.New(providererr.Transient, "sandbox_bad_response", "fc-manager 返回的环境 id 格式不对")
	}
	if !envIDPattern.MatchString(out.EnvID) {
		return sandbox.Env{}, providererr.New(providererr.Transient, "sandbox_bad_response", "fc-manager 返回的环境 id 格式不对")
	}
	profileMatches := out.GuestProfile == b.p.cfg.GuestProfile && out.RootDiskMB == b.p.cfg.RootDiskMB
	if b.p.cfg.GuestProfile == "workspace" && out.GuestProfile == "" && out.RootDiskMB == 0 {
		profileMatches = true // Existing managers omit profile fields for standard guests.
	}
	if !profileMatches {
		return sandbox.Env{}, b.rejectCreatedProfile(ctx, out.EnvID)
	}
	// fc-manager 在回 201 之前已经等到 agent 的 /healthz。
	image := out.Image
	if prepared.SourceDigest != "" {
		if out.Image != prepared.SourceDigest {
			cleanupErr := b.Destroy(context.WithoutCancel(ctx), out.EnvID)
			if cleanupErr != nil {
				return sandbox.Env{}, providererr.New(providererr.Transient, "image_identity_mismatch", "Firecracker rootfs 摘要与登记不符，拒绝执行；VM "+out.EnvID+" 清理需重试")
			}
			return sandbox.Env{}, providererr.New(providererr.Invalid, "image_identity_mismatch", "Firecracker rootfs 摘要与登记不符，已销毁 VM")
		}
		if spec.Image != "" {
			image = spec.Image
		}
	}
	return sandbox.Env{ID: out.EnvID, Spec: spec, Image: image, HealthConfirmed: true}, nil
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

// PreviewAddress 是 fc-manager 为这个客户机端口开的入口（宿主上的 IP:端口）。
func (b *box) PreviewAddress(ctx context.Context, envID string, port int) (string, error) {
	if port < 1 || port > 65535 || port == 3128 || port == 3129 {
		return "", providererr.New(providererr.Invalid, "invalid_port", "端口要在 1 到 65535 之间，且不能是客户机里出网转发器的端口")
	}
	if !envIDPattern.MatchString(envID) {
		return "", providererr.New(providererr.NotFound, "unknown_env", "环境不存在")
	}
	status, body, err := b.p.call(ctx, http.MethodPost, "/v1/envs/"+envID+"/preview", b.owner, map[string]int{"port": port})
	if err != nil {
		return "", err
	}
	if err := sandboxagent.RaiseFor(status, body); err != nil {
		return "", err
	}
	var out struct {
		Address string `json:"address"`
	}
	if json.Unmarshal(body, &out) != nil {
		return "", providererr.New(providererr.Transient, "sandbox_bad_response", "fc-manager 返回的预览地址格式不对")
	}
	if _, _, err := net.SplitHostPort(out.Address); err != nil {
		return "", providererr.New(providererr.Transient, "sandbox_bad_response", "fc-manager 返回的预览地址格式不对")
	}
	return out.Address, nil
}

func (b *box) Destroy(ctx context.Context, envID string) error {
	if err := b.p.faults.Check("destroy"); err != nil {
		return err
	}
	if !envIDPattern.MatchString(envID) {
		return providererr.New(providererr.NotFound, "unknown_env", "环境不存在")
	}
	status, body, err := b.p.call(ctx, http.MethodDelete, "/v1/envs/"+envID, b.owner, nil)
	if err != nil {
		return err
	}
	return sandboxagent.RaiseFor(status, body)
}
