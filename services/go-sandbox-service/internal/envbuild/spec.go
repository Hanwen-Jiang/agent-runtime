package envbuild

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/kube"
)

// RegistrySecret 是推送环境镜像的凭据（SecretStore 平台作用域：{"username","password"}）。
const RegistrySecret = "sandbox/env-registry"

// LazyRegistry 是镜像仓库的客户端，第一次推或删时才读凭据。
type LazyRegistry struct {
	url, pullHost string
	readSecret    func(ctx context.Context, name string) ([]byte, error)

	mu     sync.Mutex
	client *RegistryClient
}

// NewLazyRegistry 构造。
func NewLazyRegistry(url, pullHost string, readSecret func(ctx context.Context, name string) ([]byte, error)) *LazyRegistry {
	return &LazyRegistry{url: url, pullHost: pullHost, readSecret: readSecret}
}

func (r *LazyRegistry) get(ctx context.Context) (*RegistryClient, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.client != nil {
		return r.client, nil
	}
	raw, err := r.readSecret(ctx, RegistrySecret)
	if err != nil {
		return nil, err
	}
	var creds struct{ Username, Password string }
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &creds)
	}
	c, err := NewRegistryClient(r.url, creds.Username, creds.Password, r.pullHost)
	if err != nil {
		return nil, err
	}
	r.client = c
	return c, nil
}

// Reference 是推好的镜像的引用。
func (r *LazyRegistry) Reference(repo, digest string) string {
	return r.pullHost + "/" + repo + "@" + digest
}

// Push 见 RegistryClient.Push。
func (r *LazyRegistry) Push(ctx context.Context, repo, tag string, l *Layout) (string, error) {
	c, err := r.get(ctx)
	if err != nil {
		return "", err
	}
	return c.Push(ctx, repo, tag, l)
}

// Delete 见 RegistryClient.Delete。
func (r *LazyRegistry) Delete(ctx context.Context, repo, digest string) (bool, error) {
	c, err := r.get(ctx)
	if err != nil {
		return false, err
	}
	return c.Delete(ctx, repo, digest)
}

// Deps 是 FromSpec 要的外部依赖。
type Deps struct {
	// ReadSecret 读 SecretStore 平台作用域的密钥（registry.Registry.ReadSecret）。
	ReadSecret func(ctx context.Context, name string) ([]byte, error)
	// Kube 按 kubeconfig 的路径连 Kubernetes（空时在集群内）；nil 时用 kube.Load。
	Kube   func(kubeconfig string) (Kube, error)
	TmpDir string
	Logger *slog.Logger
}

// FromSpec 按后端登记文件的 environments 一节建 Manager（格式不变）：
//
//	"environments": {
//	  "base_image": "ghcr.io/agentruntime/sandbox-universal:0.1.0", "base_digest": "sha256:…",
//	  "registry": {"url": "http://…-env-registry.agent-runtime.svc:5000", "pull_host": "127.0.0.1:30500", "prefix": "envs"},
//	  "builder": {"kind": "k8s", "namespace": "sandbox-builds", "runtime_class": "kata", "isolation": "microvm",
//	              "agent_image": "ghcr.io/agentruntime/sandbox-agent:0.1.0", "workspace_claim": "workspaces"},
//	  "build_hosts": ["registry-1.docker.io", "auth.docker.io", "production.cloudflare.docker.com", "deb.debian.org"],
//	  "limits": {"max_images": 5, "max_image_bytes": 8589934592, "build_timeout_s": 1800, "idle_days": 14,
//	             "failed_retry_after_s": 3600, "max_concurrent": 4}
//	}
func FromSpec(spec map[string]any, deps Deps) (*Manager, error) {
	str := func(m map[string]any, key string) string { v, _ := m[key].(string); return v }
	num := func(m map[string]any, key string) (int64, bool) {
		v, ok := m[key].(float64)
		return int64(v), ok
	}
	base := str(spec, "base_image")
	if base == "" {
		return nil, errors.New("environments.base_image 必填")
	}
	reg, ok := spec["registry"].(map[string]any)
	if !ok || str(reg, "url") == "" || str(reg, "pull_host") == "" {
		return nil, errors.New("environments.registry 要有 url 与 pull_host")
	}
	prefix := "envs"
	if v, ok := reg["prefix"].(string); ok {
		prefix = v
	}
	path := RegistryPath{Host: str(reg, "pull_host"), Prefix: strings.Trim(prefix, "/")}
	limits := DefaultLimits()
	if l, ok := spec["limits"].(map[string]any); ok {
		if v, ok := num(l, "max_images"); ok {
			limits.MaxImages = int(v)
		}
		if v, ok := num(l, "max_image_bytes"); ok && v > 0 {
			limits.MaxImageBytes = v
		}
		if v, ok := num(l, "build_timeout_s"); ok && v > 0 {
			limits.BuildTimeout = time.Duration(v) * time.Second
		}
		if v, ok := num(l, "idle_days"); ok && v > 0 {
			limits.IdleDays = int(v)
		}
		if v, ok := num(l, "failed_retry_after_s"); ok && v > 0 {
			limits.FailedRetryAfter = time.Duration(v) * time.Second
		}
		if v, ok := num(l, "max_concurrent"); ok && v > 0 {
			limits.MaxConcurrent = int(v)
		}
	}
	var builder Builder
	if b, ok := spec["builder"].(map[string]any); ok && len(b) > 0 {
		if str(b, "kind") != "k8s" {
			return nil, errors.New("environments.builder.kind 只能是 k8s")
		}
		load := deps.Kube
		if load == nil {
			load = func(kubeconfig string) (Kube, error) { return kube.Load(kubeconfig) }
		}
		api, err := load(str(b, "kubeconfig"))
		if err != nil {
			return nil, fmt.Errorf("environments.builder：%w", err)
		}
		jb, err := BuilderFromSpec(b, api)
		if err != nil {
			return nil, fmt.Errorf("environments.builder：%w", err)
		}
		builder = jb
	}
	var hosts []string
	if list, ok := spec["build_hosts"].([]any); ok {
		for _, v := range list {
			if s, ok := v.(string); ok && s != "" {
				hosts = append(hosts, s)
			}
		}
	}
	pusher := NewLazyRegistry(str(reg, "url"), path.Host, deps.ReadSecret)
	cfg := Config{BaseImage: base, BaseDigest: str(spec, "base_digest"), Registry: path, Pusher: pusher, Limits: limits, BuildHosts: hosts,
		TmpDir: deps.TmpDir, Logger: deps.Logger}
	if builder != nil {
		cfg.Builder = builder
	}
	return New(cfg), nil
}
