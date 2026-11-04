// Package k8ssandbox 是 Sandbox 的 K8s 实现：每个环境一个一次性 Pod（设计书 4.10、附录 L.11；D-05）。语义见 providers/sandbox。
// SBX-01 从 Python 的 agentruntime_core.adapters.k8s_sandbox 搬来，Pod 的样子与标签、注解、令牌算法都不变：
// 搬迁前由 Worker 建的 Pod，上线后 Sandbox Service 照常接管。
//
// Pod 的样子：
//   - RuntimeClass 由配置给出（gvisor 是容器级隔离，kata 是 microVM 级）；空串用集群默认，只给开发与测试。
//   - initContainer 用 agent 镜像把 sandbox-agent 拷进一个 emptyDir，跑命令的容器以它为入口；命令、读写文件都经 agent。
//   - 非 root、根文件系统只读、去掉全部 capability、不许提权、seccomp RuntimeDefault、不挂 ServiceAccount 令牌、
//     不注入 Service 的环境变量；/tmp 与工作目录是 emptyDir。
//   - 网络：只支持断网（标签 agentruntime.io/network=none，命名空间里默认拒绝出网的 NetworkPolicy 起作用）。
//   - WorkspaceID 非空时，工作目录挂沙箱命名空间里的 PVC（WorkspaceClaim）的 subPath <tenant>/<workspace>。
//
// 作用域：Pod 的注解里记着所属作用域，标签里是它的哈希；别的作用域访问一律 not_found。
// agent 的令牌由 TokenKey 对 env_id 做 HMAC 得到，不存放：同一套配置的任何服务副本都能连上。
// 空闲超时：注解里记着到期时间，每次用时顺延；新建环境时顺手删掉到期的 Pod（最多每分钟一次）。activeDeadlineSeconds 兜底。
// MaxEnvsPerScope：新建前按标签数一数这个作用域还活着的 Pod，到上限就 denied（sandbox_quota_exceeded，软上限）。
package k8ssandbox

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/kube"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/sandboxagent"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// 标签与注解（与 Python 版相同）。
const (
	ManagedBy         = "agentruntime-sandbox"
	ManagedByLabel    = "app.kubernetes.io/managed-by"
	OwnerLabel        = "agentruntime.io/owner"
	NetworkLabel      = "agentruntime.io/network"
	OwnerAnnotation   = "agentruntime.io/owner"
	ExpiresAnnotation = "agentruntime.io/expires-at"
	IdleSecondsAnnot  = "agentruntime.io/idle-seconds"
	timeLayout        = "2006-01-02T15:04:05Z"
	connectPodIP      = "pod_ip"
	connectAPIProxy   = "api_proxy"
)

var envIDPattern = regexp.MustCompile(`^sbx-[0-9a-f]{16}$`)

// 拉镜像失败、配置不对、一直起不来：不再等，删掉 Pod 报 transient。
var fatalWaiting = map[string]bool{"ErrImagePull": true, "ImagePullBackOff": true, "InvalidImageName": true, "CreateContainerConfigError": true,
	"CreateContainerError": true, "CrashLoopBackOff": true}

// Config 是一个 K8s 后端的配置（后端登记里的字段）。
type Config struct {
	Namespace       string
	Images          map[string]string // runtime → 跑命令的镜像
	AgentImage      string
	RuntimeClass    string
	Connect         string // pod_ip、api_proxy
	AgentPort       int
	WorkspaceClaim  string
	RunAsUser       int
	ImagePullPolicy string
	TmpSizeMB       int
	StartupTimeout  time.Duration
	MaxLifetime     time.Duration
	MaxEnvsPerScope int
	// CustomImagePrefix：Spec.Image 只接受这个前缀下、属于本作用域的引用（<前缀>/<租户>/env@sha256:…，D-17）。
	CustomImagePrefix string
}

func (c *Config) defaults() {
	if c.Namespace == "" {
		c.Namespace = "sandboxes"
	}
	if len(c.Images) == 0 {
		c.Images = map[string]string{"python": "python:3.13-slim"}
	}
	if c.AgentImage == "" {
		c.AgentImage = "ghcr.io/agentruntime/sandbox-agent:0.1.0"
	}
	if c.Connect == "" {
		c.Connect = connectPodIP
	}
	if c.AgentPort == 0 {
		c.AgentPort = 7070
	}
	if c.RunAsUser == 0 {
		c.RunAsUser = 10001
	}
	if c.ImagePullPolicy == "" {
		c.ImagePullPolicy = "IfNotPresent"
	}
	if c.TmpSizeMB == 0 {
		c.TmpSizeMB = 512
	}
	if c.StartupTimeout == 0 {
		c.StartupTimeout = 2 * time.Minute
	}
	if c.MaxLifetime == 0 {
		c.MaxLifetime = 6 * time.Hour
	}
}

// Provider 是 K8s Sandbox 的工厂。
type Provider struct {
	kube     *kube.API
	cfg      Config
	tokenKey []byte
	faults   *testkit.Faults
	agent    sandboxagent.Client

	mu        sync.Mutex
	destroyed map[string]string
	lastReap  time.Time
	lastTouch map[string]time.Time
}

// New 构造 K8s Sandbox。tokenKey 为空时每个进程随机一把（只有一个副本时可以）。
func New(api *kube.API, cfg Config, tokenKey []byte, faults *testkit.Faults) (*Provider, error) {
	cfg.defaults()
	if cfg.Connect != connectPodIP && cfg.Connect != connectAPIProxy {
		return nil, errors.New("connect 只能是 pod_ip 或 api_proxy")
	}
	if len(tokenKey) == 0 {
		tokenKey = make([]byte, 32)
		_, _ = rand.Read(tokenKey)
	}
	if faults == nil {
		faults = testkit.NewFaults()
	}
	return &Provider{kube: api, cfg: cfg, tokenKey: tokenKey, faults: faults, destroyed: map[string]string{}, lastTouch: map[string]time.Time{}}, nil
}

// Bind 绑定租户作用域。
func (p *Provider) Bind(t *commonv1.TenantContext) (sandbox.Sandbox, error) {
	scope, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return p.box(scope.TenantID, true), nil
}

// BindPlatform 绑定平台作用域。
func (p *Provider) BindPlatform(purpose string) (sandbox.Sandbox, error) {
	if _, err := tenancy.PlatformScope(purpose); err != nil {
		return nil, err
	}
	return p.box(devutil.PlatformID, false), nil
}

func (p *Provider) box(owner string, tenant bool) *box {
	sum := sha256.Sum256([]byte(owner))
	return &box{p: p, owner: owner, ownerLabel: hex.EncodeToString(sum[:])[:40], tenant: tenant}
}

// Token 是 agent 的令牌（HMAC(token_key, env_id) 的十六进制）。
func (p *Provider) Token(envID string) string {
	m := hmac.New(sha256.New, p.tokenKey)
	m.Write([]byte(envID))
	return hex.EncodeToString(m.Sum(nil))
}

func (p *Provider) podsPath(name string) string {
	base := "/api/v1/namespaces/" + p.cfg.Namespace + "/pods"
	if name != "" {
		return base + "/" + name
	}
	return base
}

type pod struct {
	Metadata struct {
		Name              string            `json:"name"`
		Labels            map[string]string `json:"labels"`
		Annotations       map[string]string `json:"annotations"`
		CreationTimestamp string            `json:"creationTimestamp"`
		DeletionTimestamp string            `json:"deletionTimestamp"`
	} `json:"metadata"`
	Status struct {
		Phase      string `json:"phase"`
		PodIP      string `json:"podIP"`
		Conditions []struct {
			Type   string `json:"type"`
			Status string `json:"status"`
		} `json:"conditions"`
		InitContainerStatuses []containerStatus `json:"initContainerStatuses"`
		ContainerStatuses     []containerStatus `json:"containerStatuses"`
	} `json:"status"`
}

type containerStatus struct {
	Name    string `json:"name"`
	ImageID string `json:"imageID"`
	State   struct {
		Waiting struct {
			Reason string `json:"reason"`
		} `json:"waiting"`
	} `json:"state"`
}

func (p *Provider) endpoint(envID string, pd *pod) (sandboxagent.Endpoint, error) {
	token := p.Token(envID)
	if p.cfg.Connect == connectAPIProxy {
		return sandboxagent.Endpoint{BaseURL: p.kube.PodProxyURL(p.cfg.Namespace, envID, p.cfg.AgentPort), Token: token,
			Headers: p.kube.AuthHeaders(), Client: p.kube.HTTPClient()}, nil
	}
	if pd.Status.PodIP == "" {
		return sandboxagent.Endpoint{}, providererr.New(providererr.Transient, "sandbox_unavailable", "沙箱 Pod 还没有地址")
	}
	return sandboxagent.Endpoint{BaseURL: "http://" + net.JoinHostPort(pd.Status.PodIP, strconv.Itoa(p.cfg.AgentPort)), Token: token}, nil
}

func managedSelector(extra map[string]string) string {
	labels := map[string]string{ManagedByLabel: ManagedBy}
	for k, v := range extra {
		labels[k] = v
	}
	return kube.LabelSelector(labels)
}

// Reap 删掉空闲到期的沙箱 Pod，返回删了几个。新建环境时顺手调（最多每分钟一次）。
func (p *Provider) Reap(ctx context.Context, force bool) (int, error) {
	p.mu.Lock()
	if !force && time.Since(p.lastReap) < time.Minute {
		p.mu.Unlock()
		return 0, nil
	}
	p.lastReap = time.Now()
	p.mu.Unlock()
	var list struct {
		Items []pod `json:"items"`
	}
	if err := p.kube.JSON(ctx, http.MethodGet, p.podsPath("")+"?labelSelector="+managedSelector(nil), nil, &list); err != nil {
		return 0, err
	}
	n := 0
	for _, it := range list.Items {
		expires := it.Metadata.Annotations[ExpiresAnnotation]
		if expires != "" && parseTime(expires).Before(time.Now().UTC()) && it.Metadata.DeletionTimestamp == "" {
			if err := p.delete(ctx, it.Metadata.Name); err != nil {
				return n, err
			}
			n++
		}
	}
	return n, nil
}

// ListEnvironments 是本部署建的沙箱 Pod（按 managed-by 标签；正在删除的不算），给对账用（D-11）。
func (p *Provider) ListEnvironments(ctx context.Context) ([]sandbox.ListedEnvironment, error) {
	var list struct {
		Items []pod `json:"items"`
	}
	if err := p.kube.JSON(ctx, http.MethodGet, p.podsPath("")+"?labelSelector="+managedSelector(nil), nil, &list); err != nil {
		return nil, err
	}
	var out []sandbox.ListedEnvironment
	for _, it := range list.Items {
		name := it.Metadata.Name
		if !envIDPattern.MatchString(name) || it.Metadata.DeletionTimestamp != "" {
			continue
		}
		state := strings.ToLower(it.Status.Phase)
		if it.Status.Phase == "Pending" || it.Status.Phase == "Running" {
			state = "running"
		}
		var created time.Time
		if it.Metadata.CreationTimestamp != "" {
			created = parseTime(it.Metadata.CreationTimestamp)
		}
		out = append(out, sandbox.ListedEnvironment{ID: name, Owner: it.Metadata.Annotations[OwnerAnnotation], CreatedAt: created, State: state})
	}
	return out, nil
}

func (p *Provider) delete(ctx context.Context, name string) error {
	status, _, err := p.kube.Request(ctx, http.MethodDelete, p.podsPath(name), map[string]any{"gracePeriodSeconds": 0, "propagationPolicy": "Background"}, "")
	if err != nil {
		return err
	}
	if status != 200 && status != 202 && status != 404 {
		return providererr.New(providererr.Transient, "sandbox_unavailable", fmt.Sprintf("删不掉沙箱 Pod（HTTP %d）", status))
	}
	return nil
}

type box struct {
	p          *Provider
	owner      string
	ownerLabel string
	tenant     bool
}

func (b *box) mine(pd *pod) bool {
	return pd.Metadata.Labels[ManagedByLabel] == ManagedBy && pd.Metadata.Annotations[OwnerAnnotation] == b.owner
}

func unknown() error { return providererr.New(providererr.NotFound, "unknown_env", "环境不存在") }

func (b *box) lookup(ctx context.Context, envID string) (*pod, error) {
	p := b.p
	p.mu.Lock()
	_, gone := p.destroyed[envID]
	p.mu.Unlock()
	if !envIDPattern.MatchString(envID) || gone {
		return nil, unknown()
	}
	var pd pod
	if err := p.kube.JSON(ctx, http.MethodGet, p.podsPath(envID), nil, &pd); err != nil {
		if providererr.CategoryOf(err) == providererr.NotFound {
			return nil, unknown()
		}
		return nil, err
	}
	if !b.mine(&pd) || pd.Metadata.DeletionTimestamp != "" {
		return nil, unknown()
	}
	return &pd, nil
}

func (b *box) Create(ctx context.Context, spec sandbox.Spec) (sandbox.Env, error) {
	p, c := b.p, b.p.cfg
	if err := p.faults.Check("create"); err != nil {
		return sandbox.Env{}, err
	}
	runtime := spec.Runtime
	if runtime == "" {
		runtime = "python"
	}
	image := c.Images[runtime]
	if spec.Image != "" {
		own := strings.TrimRight(c.CustomImagePrefix, "/") + "/" + b.owner + "/env@sha256:"
		if c.CustomImagePrefix == "" || !b.tenant || !strings.HasPrefix(spec.Image, own) {
			return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_image", "只接受本租户在环境镜像仓库里、按 digest 写的镜像")
		}
		image = spec.Image
	}
	if image == "" {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_runtime", "没有配置 runtime "+runtime+" 的镜像")
	}
	if spec.Network != "" && spec.Network != "none" {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "network_allowlist_unsupported", "K8s 沙箱只支持断网；按域名放行要用 microVM 后端")
	}
	if spec.CPUMillis <= 0 || spec.MemoryMB <= 0 || spec.IdleTimeout <= 0 {
		return sandbox.Env{}, providererr.New(providererr.Invalid, "invalid_argument", "cpu_millis、memory_mb、idle_timeout 都要大于 0")
	}
	subPath := ""
	if spec.WorkspaceID != "" {
		if c.WorkspaceClaim == "" {
			return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_workspace", "这个 Sandbox 没有配置工作区，不能绑定 workspace")
		}
		if !b.tenant {
			return sandbox.Env{}, providererr.New(providererr.Invalid, "unsupported_workspace", "平台作用域没有 workspace")
		}
		if err := sandbox.CheckWorkspaceID(spec.WorkspaceID); err != nil {
			return sandbox.Env{}, err
		}
		subPath = b.owner + "/" + spec.WorkspaceID
	}
	if _, err := p.Reap(ctx, false); err != nil {
		return sandbox.Env{}, err
	}
	if c.MaxEnvsPerScope > 0 {
		var list struct {
			Items []pod `json:"items"`
		}
		if err := p.kube.JSON(ctx, http.MethodGet, p.podsPath("")+"?labelSelector="+managedSelector(map[string]string{OwnerLabel: b.ownerLabel}), nil, &list); err != nil {
			return sandbox.Env{}, err
		}
		alive := 0
		for _, it := range list.Items {
			if it.Metadata.DeletionTimestamp == "" {
				alive++
			}
		}
		if alive >= c.MaxEnvsPerScope {
			return sandbox.Env{}, providererr.New(providererr.Denied, "sandbox_quota_exceeded", fmt.Sprintf("同时最多 %d 个沙箱，先等前面的跑完", c.MaxEnvsPerScope))
		}
	}
	raw := make([]byte, 8)
	_, _ = rand.Read(raw)
	envID := "sbx-" + hex.EncodeToString(raw)
	if err := p.kube.JSON(ctx, http.MethodPost, p.podsPath(""), b.manifest(envID, spec, image, subPath), nil); err != nil {
		return sandbox.Env{}, err
	}
	pd, err := b.waitReady(ctx, envID)
	if err == nil {
		var ep sandboxagent.Endpoint
		if ep, err = p.endpoint(envID, pd); err == nil {
			deadline := time.Now().Add(30 * time.Second)
			for !p.agent.Healthy(ctx, ep) {
				if time.Now().After(deadline) {
					err = providererr.New(providererr.Transient, "sandbox_unavailable", "沙箱里的 agent 没有就绪")
					break
				}
				time.Sleep(300 * time.Millisecond)
			}
		}
	}
	if err != nil {
		cleanup, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		_ = p.delete(cleanup, envID)
		return sandbox.Env{}, err
	}
	p.mu.Lock()
	p.lastTouch[envID] = time.Now()
	p.mu.Unlock()
	return sandbox.Env{ID: envID, Spec: spec, Image: imageID(pd), HealthConfirmed: true}, nil
}

func (b *box) waitReady(ctx context.Context, envID string) (*pod, error) {
	p := b.p
	deadline := time.Now().Add(p.cfg.StartupTimeout)
	for {
		var pd pod
		if err := p.kube.JSON(ctx, http.MethodGet, p.podsPath(envID), nil, &pd); err != nil {
			return nil, err
		}
		if pd.Status.Phase == "Failed" || pd.Status.Phase == "Succeeded" {
			return nil, providererr.New(providererr.Transient, "sandbox_unavailable", "沙箱 Pod 意外结束（"+pd.Status.Phase+"）")
		}
		for _, cs := range append(append([]containerStatus{}, pd.Status.InitContainerStatuses...), pd.Status.ContainerStatuses...) {
			if fatalWaiting[cs.State.Waiting.Reason] {
				return nil, providererr.New(providererr.Transient, "sandbox_unavailable", "沙箱 Pod 起不来（"+cs.State.Waiting.Reason+"）")
			}
		}
		ready := false
		for _, cond := range pd.Status.Conditions {
			if cond.Type == "Ready" && cond.Status == "True" {
				ready = true
			}
		}
		if pd.Status.Phase == "Running" && ready {
			return &pd, nil
		}
		if time.Now().After(deadline) {
			return nil, providererr.New(providererr.Transient, "sandbox_unavailable", "沙箱 Pod 在时限内没有就绪")
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
	}
}

func (b *box) touch(ctx context.Context, envID string, pd *pod) error {
	p := b.p
	p.mu.Lock()
	recent := time.Since(p.lastTouch[envID]) < time.Minute
	p.mu.Unlock()
	if recent {
		return nil
	}
	idle, err := strconv.Atoi(pd.Metadata.Annotations[IdleSecondsAnnot])
	if err != nil {
		idle = 600
	}
	patch := map[string]any{"metadata": map[string]any{"annotations": map[string]string{
		ExpiresAnnotation: time.Now().UTC().Add(time.Duration(idle) * time.Second).Format(timeLayout)}}}
	if err := p.kube.JSONType(ctx, http.MethodPatch, p.podsPath(envID), patch, "application/merge-patch+json", nil); err != nil {
		return err
	}
	p.mu.Lock()
	p.lastTouch[envID] = time.Now()
	p.mu.Unlock()
	return nil
}

func (b *box) agentFor(ctx context.Context, envID string) (sandboxagent.Endpoint, *pod, error) {
	pd, err := b.lookup(ctx, envID)
	if err != nil {
		return sandboxagent.Endpoint{}, nil, err
	}
	ep, err := b.p.endpoint(envID, pd)
	return ep, pd, err
}

func (b *box) Healthy(ctx context.Context, envID string) bool {
	ep, _, err := b.agentFor(ctx, envID)
	return err == nil && b.p.agent.Healthy(ctx, ep)
}

func (b *box) Exec(ctx context.Context, envID string, argv []string, opts sandbox.ExecOptions) (sandbox.ExecResult, error) {
	if err := b.p.faults.Check("exec"); err != nil {
		return sandbox.ExecResult{}, err
	}
	ep, pd, err := b.agentFor(ctx, envID)
	if err != nil {
		return sandbox.ExecResult{}, err
	}
	if len(argv) == 0 {
		return sandbox.ExecResult{}, providererr.New(providererr.Invalid, "invalid_argument", "argv 不能为空")
	}
	if err := b.touch(ctx, envID, pd); err != nil {
		return sandbox.ExecResult{}, err
	}
	return b.p.agent.Exec(ctx, ep, argv, opts)
}

func (b *box) WriteFile(ctx context.Context, envID, path string, data []byte) error {
	if err := b.p.faults.Check("write_file"); err != nil {
		return err
	}
	if err := sandbox.CheckPath(path); err != nil {
		return err
	}
	ep, _, err := b.agentFor(ctx, envID)
	if err != nil {
		return err
	}
	return b.p.agent.WriteFile(ctx, ep, path, data)
}

func (b *box) ReadFile(ctx context.Context, envID, path string) ([]byte, error) {
	if err := b.p.faults.Check("read_file"); err != nil {
		return nil, err
	}
	if err := sandbox.CheckPath(path); err != nil {
		return nil, err
	}
	ep, _, err := b.agentFor(ctx, envID)
	if err != nil {
		return nil, err
	}
	return b.p.agent.ReadFile(ctx, ep, path)
}

func (b *box) ExportArchive(ctx context.Context, envID string, out io.Writer, maxBytes int64) (int64, error) {
	if err := b.p.faults.Check("export_archive"); err != nil {
		return 0, err
	}
	ep, _, err := b.agentFor(ctx, envID)
	if err != nil {
		return 0, err
	}
	return b.p.agent.ExportArchive(ctx, ep, out, maxBytes)
}

func (b *box) ImportArchive(ctx context.Context, envID string, src io.Reader, size, maxBytes int64) error {
	if err := b.p.faults.Check("import_archive"); err != nil {
		return err
	}
	ep, _, err := b.agentFor(ctx, envID)
	if err != nil {
		return err
	}
	return b.p.agent.ImportArchive(ctx, ep, src, size, maxBytes)
}

func (b *box) Manifest(ctx context.Context, envID string) ([]sandbox.FileEntry, error) {
	if err := b.p.faults.Check("manifest"); err != nil {
		return nil, err
	}
	ep, _, err := b.agentFor(ctx, envID)
	if err != nil {
		return nil, err
	}
	return b.p.agent.Manifest(ctx, ep)
}

func (b *box) ListDir(ctx context.Context, envID, path string) ([]sandbox.DirEntry, error) {
	ep, _, err := b.agentFor(ctx, envID)
	if err != nil {
		return nil, err
	}
	return b.p.agent.ListDir(ctx, ep, path)
}

func (b *box) Grep(ctx context.Context, envID, pattern, path string, maxMatches int) ([]sandbox.GrepMatch, bool, error) {
	ep, _, err := b.agentFor(ctx, envID)
	if err != nil {
		return nil, false, err
	}
	return b.p.agent.Grep(ctx, ep, pattern, path, maxMatches)
}

// PreviewAddress 转发到 Pod 的这个端口（出网网关在集群里直连 Pod，D-16）。
func (b *box) PreviewAddress(ctx context.Context, envID string, port int) (string, error) {
	if port < 1 || port > 65535 || port == b.p.cfg.AgentPort {
		return "", providererr.New(providererr.Invalid, "invalid_port", "端口要在 1 到 65535 之间，且不能是 agent 的端口")
	}
	pd, err := b.lookup(ctx, envID)
	if err != nil {
		return "", err
	}
	if pd.Status.PodIP == "" {
		return "", providererr.New(providererr.Transient, "sandbox_unavailable", "沙箱 Pod 还没有地址")
	}
	return net.JoinHostPort(pd.Status.PodIP, strconv.Itoa(port)), nil
}

func (b *box) Destroy(ctx context.Context, envID string) error {
	p := b.p
	if err := p.faults.Check("destroy"); err != nil {
		return err
	}
	if !envIDPattern.MatchString(envID) {
		return unknown()
	}
	p.mu.Lock()
	owner, gone := p.destroyed[envID]
	p.mu.Unlock()
	if gone && owner == b.owner {
		return nil
	}
	status, data, err := p.kube.Request(ctx, http.MethodGet, p.podsPath(envID), nil, "")
	if err != nil {
		return err
	}
	if status == 404 {
		return nil
	}
	if status != 200 {
		return kube.ErrorFor(status, data)
	}
	var pd pod
	if err := json.Unmarshal(data, &pd); err != nil || !b.mine(&pd) {
		return unknown()
	}
	if err := p.delete(ctx, envID); err != nil {
		return err
	}
	p.mu.Lock()
	p.destroyed[envID] = b.owner
	delete(p.lastTouch, envID)
	p.mu.Unlock()
	return nil
}

func (b *box) manifest(envID string, spec sandbox.Spec, image, subPath string) map[string]any {
	c := b.p.cfg
	idle := int(spec.IdleTimeout.Seconds())
	security := map[string]any{
		"allowPrivilegeEscalation": false, "readOnlyRootFilesystem": true, "capabilities": map[string]any{"drop": []string{"ALL"}},
		"runAsNonRoot": true, "runAsUser": c.RunAsUser, "runAsGroup": c.RunAsUser,
	}
	workdirVolume := map[string]any{"name": "workdir", "emptyDir": map[string]any{}}
	workdirMount := map[string]any{"name": "workdir", "mountPath": "/workspace"}
	if subPath != "" {
		workdirVolume = map[string]any{"name": "workdir", "persistentVolumeClaim": map[string]any{"claimName": c.WorkspaceClaim}}
		workdirMount["subPath"] = subPath
	}
	cpu, mem := fmt.Sprintf("%dm", spec.CPUMillis), fmt.Sprintf("%dMi", spec.MemoryMB)
	podSpec := map[string]any{
		"automountServiceAccountToken":  false,
		"enableServiceLinks":            false,
		"restartPolicy":                 "Never",
		"activeDeadlineSeconds":         int(c.MaxLifetime.Seconds()),
		"terminationGracePeriodSeconds": 0,
		"securityContext": map[string]any{"runAsNonRoot": true, "runAsUser": c.RunAsUser, "runAsGroup": c.RunAsUser, "fsGroup": c.RunAsUser,
			"seccompProfile": map[string]any{"type": "RuntimeDefault"}},
		"initContainers": []any{map[string]any{
			"name": "install-agent", "image": c.AgentImage, "imagePullPolicy": c.ImagePullPolicy,
			"command":         []string{"/sandbox-agent", "install", "/agent/sandbox-agent"},
			"securityContext": security,
			"resources":       map[string]any{"requests": map[string]any{"cpu": "50m", "memory": "32Mi"}, "limits": map[string]any{"cpu": "200m", "memory": "64Mi"}},
			"volumeMounts":    []any{map[string]any{"name": "agent", "mountPath": "/agent"}},
		}},
		"containers": []any{map[string]any{
			"name": "sandbox", "image": image, "imagePullPolicy": c.ImagePullPolicy,
			"command": []string{"/agent/sandbox-agent"}, "workingDir": "/workspace",
			"env": []any{
				map[string]any{"name": "AGENT_TOKEN", "value": b.p.Token(envID)},
				map[string]any{"name": "AGENT_WORKDIR", "value": "/workspace"},
				map[string]any{"name": "AGENT_ADDR", "value": fmt.Sprintf(":%d", c.AgentPort)},
				map[string]any{"name": "AGENT_MAX_TIMEOUT_S", "value": strconv.Itoa(int(c.MaxLifetime.Seconds()))},
				map[string]any{"name": "HOME", "value": "/tmp"},
			},
			"ports":           []any{map[string]any{"name": "agent", "containerPort": c.AgentPort}},
			"readinessProbe":  map[string]any{"httpGet": map[string]any{"path": "/healthz", "port": c.AgentPort}, "periodSeconds": 1, "failureThreshold": 60},
			"securityContext": security,
			"resources": map[string]any{
				"requests": map[string]any{"cpu": cpu, "memory": mem},
				"limits":   map[string]any{"cpu": cpu, "memory": mem, "ephemeral-storage": "2Gi"},
			},
			"volumeMounts": []any{
				map[string]any{"name": "agent", "mountPath": "/agent", "readOnly": true},
				map[string]any{"name": "tmp", "mountPath": "/tmp"},
				workdirMount,
			},
		}},
		"volumes": []any{
			map[string]any{"name": "agent", "emptyDir": map[string]any{"sizeLimit": "64Mi"}},
			map[string]any{"name": "tmp", "emptyDir": map[string]any{"sizeLimit": fmt.Sprintf("%dMi", c.TmpSizeMB)}},
			workdirVolume,
		},
	}
	if c.RuntimeClass != "" {
		podSpec["runtimeClassName"] = c.RuntimeClass
	}
	return map[string]any{
		"apiVersion": "v1", "kind": "Pod",
		"metadata": map[string]any{
			"name": envID, "namespace": c.Namespace,
			"labels": map[string]string{ManagedByLabel: ManagedBy, OwnerLabel: b.ownerLabel, NetworkLabel: "none"},
			"annotations": map[string]string{
				OwnerAnnotation: b.owner, IdleSecondsAnnot: strconv.Itoa(idle),
				ExpiresAnnotation: time.Now().UTC().Add(spec.IdleTimeout).Format(timeLayout),
			},
		},
		"spec": podSpec,
	}
}

// Manifest 给测试看 Pod 的定义（不建）。
func (p *Provider) Manifest(owner, envID string, spec sandbox.Spec, image, subPath string) map[string]any {
	return p.box(owner, owner != devutil.PlatformID).manifest(envID, spec, image, subPath)
}

func imageID(pd *pod) string {
	for _, cs := range pd.Status.ContainerStatuses {
		if cs.Name == "sandbox" {
			return strings.TrimPrefix(cs.ImageID, "docker-pullable://")
		}
	}
	return ""
}

func parseTime(s string) time.Time {
	t, err := time.Parse(timeLayout, s)
	if err != nil {
		return time.Date(9999, 1, 1, 0, 0, 0, 0, time.UTC)
	}
	return t
}
