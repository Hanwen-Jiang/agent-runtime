package envbuild

// 按项目定制的运行环境的构建器（从 Worker 的 agentruntime_worker.sandbox.env_builder 搬来）：在 microVM 里构建不可信的 Dockerfile。
//
// K8sJobBuilder 每次构建建一个一次性的 Job（命名空间默认 sandbox-builds）：
//
//   - RuntimeClass 必须是 microVM 级的（Kata；登记里 runtime_class，建 Builder 时核对 isolation = microvm）：BuildKit 要特权才能
//     起容器，特权只在 VM 里面（Kata 配 privileged_without_host_devices，见 deploy/cluster），碰不到宿主。
//   - 不挂 ServiceAccount 令牌、不用宿主的网络、PID、IPC，不挂宿主路径；Dockerfile 由 ConfigMap 给，仓库里的文件（要上下文时）
//     以只读的 subPath 挂本租户的这个工作区（与沙箱 Pod 同一块卷），别的租户的看不到。
//   - 联网只经包镜像与出网网关：构建的命名空间默认拒绝出网，网络策略只放行这两处；代理与镜像源的变量（带这次构建的短期凭据）
//     放在这次构建自己的 Secret 里，Job 结束就删。
//   - 两步：initContainer 用 BuildKit（buildctl-daemonless.sh）构建，输出 OCI 打包到 emptyDir（sizeLimit 是镜像的大小上限，
//     超了 kubelet 驱逐）；主容器跑 sandbox-agent，只读地挂着这个 emptyDir。服务等构建那一步正常退出、agent 就绪，按 Pod IP
//     取回 image.tar（令牌是这次构建随机生成的，只在 Secret 与服务的内存里），边取边核对大小上限，取完就删 Job。构建里
//     没有镜像仓库的凭据，也不连 ObjectStore：推到仓库是服务的事（Manager）。
//   - 超时：构建那一步超过 timeout 就删 Job（另有 activeDeadlineSeconds 兜底）；失败、超时、被驱逐都给出原因（失败时附上构建
//     日志的最后几行，去掉地址与凭据）。
//   - 同一个键之前没删干净的 Job、Secret、ConfigMap（服务在构建中途重启）在下一次构建开始时按标签删掉。

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/kube"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

const (
	// ManagedBy 是构建的对象上的 app.kubernetes.io/managed-by。
	ManagedBy = "agentruntime-env-builder"
	// DefaultBuildkitImage 写死（发布满两周）。
	DefaultBuildkitImage = "moby/buildkit:v0.25.1"
	// AgentPort 是提供产物的 sandbox-agent 的端口。
	AgentPort = 7070
	artifact  = "image.tar"
)

var k8sName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// Kube 是构建器用到的 Kubernetes API（kube.API 实现；测试里是假的）。
type Kube interface {
	Request(ctx context.Context, method, path string, body any, contentType string) (int, []byte, error)
	JSON(ctx context.Context, method, path string, body any, out any) error
}

// Fetch 取回构建产物：(地址, 令牌, 写到哪里, 最多多少字节) → 写了多少字节；超过上限 invalid（image_too_large）。
type Fetch func(ctx context.Context, url, token string, out io.Writer, limit int64) (int64, error)

// BuilderConfig 是构建器的配置（后端登记里 environments.builder 一节）。
type BuilderConfig struct {
	AgentImage    string
	Namespace     string
	RuntimeClass  string
	Isolation     string
	BuildkitImage string
	// WorkspaceClaim 是沙箱用的工作区卷（与 K8s 沙箱后端的 workspace_claim 相同；在构建的命名空间里也要有同名的 PVC 指向同一个卷）。
	WorkspaceClaim  string
	CPUMillis       int
	MemoryMB        int
	ImagePullPolicy string
	PollInterval    time.Duration
	// FetchWindow 是构建结束后留多久给服务取回产物（activeDeadlineSeconds 在构建的超时之外再加这么多）。
	FetchWindow time.Duration
}

func (c BuilderConfig) withDefaults() BuilderConfig {
	if c.Namespace == "" {
		c.Namespace = "sandbox-builds"
	}
	if c.RuntimeClass == "" {
		c.RuntimeClass = "kata"
	}
	if c.Isolation == "" {
		c.Isolation = "microvm"
	}
	if c.BuildkitImage == "" {
		c.BuildkitImage = DefaultBuildkitImage
	}
	if c.CPUMillis <= 0 {
		c.CPUMillis = 2000
	}
	if c.MemoryMB <= 0 {
		c.MemoryMB = 4096
	}
	if c.ImagePullPolicy == "" {
		c.ImagePullPolicy = "IfNotPresent"
	}
	if c.PollInterval <= 0 {
		c.PollInterval = 2 * time.Second
	}
	if c.FetchWindow <= 0 {
		c.FetchWindow = 15 * time.Minute
	}
	return c
}

// K8sJobBuilder 在 Kata 里的一次性 Job 里构建。
type K8sJobBuilder struct {
	kube  Kube
	cfg   BuilderConfig
	fetch Fetch
}

// NewK8sJobBuilder 构造；不是 microVM 的配置一律拒绝。fetch 为 nil 时用 HTTPFetch。
func NewK8sJobBuilder(k Kube, cfg BuilderConfig, fetch Fetch) (*K8sJobBuilder, error) {
	cfg = cfg.withDefaults()
	if cfg.Isolation != "microvm" {
		return nil, errors.New("构建不可信的 Dockerfile 只能在 microVM 里：runtime_class 要是 Kata 一类、isolation 要是 microvm")
	}
	if !k8sName.MatchString(cfg.Namespace) {
		return nil, errors.New("构建的命名空间格式不对")
	}
	if cfg.AgentImage == "" {
		return nil, errors.New("构建器要 agent_image（提供产物的 sandbox-agent）")
	}
	if fetch == nil {
		fetch = HTTPFetch
	}
	return &K8sJobBuilder{kube: k, cfg: cfg, fetch: fetch}, nil
}

// JobName 是这次构建的对象名。
func JobName(key string) string {
	b := make([]byte, 3)
	_, _ = rand.Read(b)
	return "envb-" + key[:16] + "-" + hex.EncodeToString(b)
}

func ownerLabel(owner string) string {
	sum := sha256.Sum256([]byte(owner))
	return hex.EncodeToString(sum[:])[:40]
}

// keyLabel 是按键找回之前没删干净的对象用的标签（标签值最长 63）。
func keyLabel(key string) string {
	if len(key) > 32 {
		return key[:32]
	}
	return key
}

// BuildScript 是构建那一步跑的脚本：联网的变量作为构建参数传给 BuildKit。
const BuildScript = `set -eu
args=""
for v in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy PIP_INDEX_URL PIP_TRUSTED_HOST UV_DEFAULT_INDEX npm_config_registry GOPROXY; do
  eval "val=\${$v:-}"
  if [ -n "$val" ]; then args="$args --opt build-arg:$v=$val"; fi
done
# shellcheck disable=SC2086
buildctl-daemonless.sh build --frontend dockerfile.v0 \
  --local context=/context --local dockerfile=/dockerfile --opt filename=Dockerfile $args \
  --output type=oci,dest=/out/image.tar
`

var envName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// Manifests 是这次构建的 ConfigMap（Dockerfile）、Secret（取回产物的令牌与联网变量）与 Job。
func (b *K8sJobBuilder) Manifests(job BuildJob, name, token string) (configMap, secret, jobManifest map[string]any, err error) {
	c := b.cfg
	labels := map[string]any{"app.kubernetes.io/managed-by": ManagedBy, "agentruntime.io/owner": ownerLabel(job.Owner), "agentruntime.io/network": "build",
		"agentruntime.io/env-key": keyLabel(job.Key)}
	meta := map[string]any{"name": name, "namespace": c.Namespace, "labels": labels, "annotations": map[string]any{"agentruntime.io/env-key": job.Key}}
	configMap = map[string]any{"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta, "data": map[string]any{"Dockerfile": job.Dockerfile}}
	netEnv := map[string]any{}
	var netNames []string
	for k, v := range job.Env {
		if envName.MatchString(k) && !strings.HasPrefix(k, "AGENT_") {
			netEnv[k] = v
			netNames = append(netNames, k)
		}
	}
	sort.Strings(netNames)
	data := map[string]any{"AGENT_TOKEN": token}
	for k, v := range netEnv {
		data[k] = v
	}
	secret = map[string]any{"apiVersion": "v1", "kind": "Secret", "metadata": meta, "type": "Opaque", "stringData": data}
	// 构建只拿联网的变量；令牌只给提供产物的那一步。
	buildEnv := []any{}
	for _, k := range netNames {
		buildEnv = append(buildEnv, map[string]any{"name": k, "valueFrom": map[string]any{"secretKeyRef": map[string]any{"name": name, "key": k}}})
	}
	unprivileged := map[string]any{"allowPrivilegeEscalation": false, "readOnlyRootFilesystem": true, "capabilities": map[string]any{"drop": []any{"ALL"}},
		"runAsNonRoot": true, "runAsUser": 10001, "runAsGroup": 10001}
	contextVolume := map[string]any{"name": "context", "emptyDir": map[string]any{"sizeLimit": "16Mi"}}
	contextMount := map[string]any{"name": "context", "mountPath": "/context", "readOnly": true}
	if job.Context != "" {
		if c.WorkspaceClaim == "" {
			return nil, nil, nil, providererr.New(providererr.Invalid, "unsupported_workspace", "构建要用仓库里的文件，但构建器没有配置工作区卷")
		}
		ws := job.WorkspaceID
		if ws == "" {
			ws = "default"
		}
		if !k8sName.MatchString(job.Owner) || !k8sName.MatchString(ws) {
			return nil, nil, nil, providererr.New(providererr.Invalid, "invalid_workspace", "租户或 workspace 的格式不对")
		}
		sub := job.Owner + "/" + ws
		if job.Context != "." {
			parts := strings.Split(job.Context, "/")
			for _, p := range parts {
				if p == ".." {
					return nil, nil, nil, providererr.New(providererr.Invalid, "invalid_context", "构建的上下文出了仓库")
				}
			}
			if strings.HasPrefix(job.Context, "/") {
				return nil, nil, nil, providererr.New(providererr.Invalid, "invalid_context", "构建的上下文出了仓库")
			}
			sub += "/" + job.Context
		}
		contextVolume = map[string]any{"name": "context", "persistentVolumeClaim": map[string]any{"claimName": c.WorkspaceClaim, "readOnly": true}}
		contextMount = map[string]any{"name": "context", "mountPath": "/context", "readOnly": true, "subPath": sub}
	}
	deadline := int64((job.Timeout + c.FetchWindow).Seconds())
	podSpec := map[string]any{
		"runtimeClassName":              c.RuntimeClass,
		"automountServiceAccountToken":  false,
		"enableServiceLinks":            false,
		"hostNetwork":                   false,
		"hostPID":                       false,
		"hostIPC":                       false,
		"restartPolicy":                 "Never",
		"terminationGracePeriodSeconds": 0,
		"initContainers": []any{map[string]any{
			"name": "build", "image": c.BuildkitImage, "imagePullPolicy": c.ImagePullPolicy,
			"command": []any{"sh", "-c", BuildScript}, "env": buildEnv,
			// 特权只在 Kata 的 VM 里（privileged_without_host_devices），碰不到宿主的设备。
			"securityContext": map[string]any{"privileged": true},
			"resources": map[string]any{
				"requests": map[string]any{"cpu": fmt.Sprintf("%dm", c.CPUMillis), "memory": fmt.Sprintf("%dMi", c.MemoryMB)},
				"limits": map[string]any{"cpu": fmt.Sprintf("%dm", c.CPUMillis), "memory": fmt.Sprintf("%dMi", c.MemoryMB),
					"ephemeral-storage": fmt.Sprintf("%dMi", job.MaxBytes*3/(1<<20))},
			},
			"volumeMounts": []any{contextMount, map[string]any{"name": "dockerfile", "mountPath": "/dockerfile", "readOnly": true},
				map[string]any{"name": "out", "mountPath": "/out"}},
		}},
		"containers": []any{map[string]any{
			// 只读地提供产物：服务带着令牌来取（GET /v1/files/image.tar）。
			"name": "serve", "image": c.AgentImage, "imagePullPolicy": c.ImagePullPolicy, "command": []any{"/sandbox-agent"},
			"env": []any{
				map[string]any{"name": "AGENT_TOKEN", "valueFrom": map[string]any{"secretKeyRef": map[string]any{"name": name, "key": "AGENT_TOKEN"}}},
				map[string]any{"name": "AGENT_WORKDIR", "value": "/out"},
				map[string]any{"name": "AGENT_ADDR", "value": fmt.Sprintf(":%d", AgentPort)},
				map[string]any{"name": "AGENT_MAX_TIMEOUT_S", "value": "30"},
			},
			"ports":           []any{map[string]any{"name": "agent", "containerPort": AgentPort}},
			"readinessProbe":  map[string]any{"httpGet": map[string]any{"path": "/healthz", "port": AgentPort}, "periodSeconds": 2},
			"securityContext": unprivileged,
			"resources":       map[string]any{"requests": map[string]any{"cpu": "100m", "memory": "64Mi"}, "limits": map[string]any{"cpu": "1", "memory": "256Mi"}},
			"volumeMounts":    []any{map[string]any{"name": "out", "mountPath": "/out", "readOnly": true}},
		}},
		"volumes": []any{
			contextVolume,
			map[string]any{"name": "dockerfile", "configMap": map[string]any{"name": name}},
			map[string]any{"name": "out", "emptyDir": map[string]any{"sizeLimit": fmt.Sprintf("%dMi", job.MaxBytes/(1<<20))}},
		},
	}
	jobManifest = map[string]any{
		"apiVersion": "batch/v1", "kind": "Job", "metadata": meta,
		"spec": map[string]any{
			"backoffLimit": 0, "activeDeadlineSeconds": deadline, "ttlSecondsAfterFinished": 600,
			"template": map[string]any{"metadata": map[string]any{"labels": labels}, "spec": podSpec},
		},
	}
	return configMap, secret, jobManifest, nil
}

func (b *K8sJobBuilder) path(kind, name string) string {
	group := "/api/v1"
	if kind == "jobs" {
		group = "/apis/batch/v1"
	}
	base := group + "/namespaces/" + b.cfg.Namespace + "/" + kind
	if name != "" {
		return base + "/" + name
	}
	return base
}

func newToken() string {
	b := make([]byte, 32)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

// Build 构建一次。
func (b *K8sJobBuilder) Build(ctx context.Context, job BuildJob, out io.Writer) (Outcome, error) {
	name := JobName(job.Key)
	token := newToken()
	cm, secret, manifest, err := b.Manifests(job, name, token)
	if err != nil {
		return Outcome{}, err
	}
	b.sweep(ctx, job.Key)
	if err := b.kube.JSON(ctx, http.MethodPost, b.path("configmaps", ""), cm, nil); err != nil {
		return Outcome{}, err
	}
	defer b.cleanup(ctx, name)
	if err := b.kube.JSON(ctx, http.MethodPost, b.path("secrets", ""), secret, nil); err != nil {
		return Outcome{}, err
	}
	if err := b.kube.JSON(ctx, http.MethodPost, b.path("jobs", ""), manifest, nil); err != nil {
		return Outcome{}, err
	}
	return b.wait(ctx, name, job, token, out)
}

func (b *K8sJobBuilder) cleanup(ctx context.Context, name string) {
	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	for _, kind := range []string{"jobs", "secrets", "configmaps"} {
		var body any
		if kind == "jobs" {
			body = map[string]any{"propagationPolicy": "Background"}
		}
		_, _, _ = b.kube.Request(cctx, http.MethodDelete, b.path(kind, name), body, "")
	}
}

// sweep 删掉同一个键之前没删干净的对象（服务在构建中途重启时留下的）。只列 Job 与 ConfigMap（服务读不了 Secret），三者同名，
// Secret 按名字删。
func (b *K8sJobBuilder) sweep(ctx context.Context, key string) {
	selector := "?labelSelector=" + kube.LabelSelector(map[string]string{"app.kubernetes.io/managed-by": ManagedBy, "agentruntime.io/env-key": keyLabel(key)})
	names := map[string]bool{}
	for _, kind := range []string{"jobs", "configmaps"} {
		var list struct {
			Items []struct {
				Metadata struct {
					Name string `json:"name"`
				} `json:"metadata"`
			} `json:"items"`
		}
		if err := b.kube.JSON(ctx, http.MethodGet, b.path(kind, "")+selector, nil, &list); err != nil {
			continue
		}
		for _, item := range list.Items {
			names[item.Metadata.Name] = true
		}
	}
	sorted := make([]string, 0, len(names))
	for n := range names {
		sorted = append(sorted, n)
	}
	sort.Strings(sorted)
	for _, n := range sorted {
		b.cleanup(ctx, n)
	}
}

type containerStatus struct {
	Name  string `json:"name"`
	Ready bool   `json:"ready"`
	State struct {
		Terminated *struct {
			ExitCode int    `json:"exitCode"`
			Reason   string `json:"reason"`
		} `json:"terminated"`
	} `json:"state"`
}

type pod struct {
	Metadata struct {
		Name string `json:"name"`
	} `json:"metadata"`
	Status struct {
		PodIP                 string            `json:"podIP"`
		Reason                string            `json:"reason"`
		InitContainerStatuses []containerStatus `json:"initContainerStatuses"`
		ContainerStatuses     []containerStatus `json:"containerStatuses"`
	} `json:"status"`
}

func (b *K8sJobBuilder) pod(ctx context.Context, name string) (*pod, error) {
	var list struct {
		Items []pod `json:"items"`
	}
	if err := b.kube.JSON(ctx, http.MethodGet, b.path("pods", "")+"?labelSelector=job-name%3D"+name, nil, &list); err != nil {
		return nil, err
	}
	if len(list.Items) == 0 {
		return nil, nil
	}
	return &list.Items[0], nil
}

func find(list []containerStatus, name string) containerStatus {
	for _, s := range list {
		if s.Name == name {
			return s
		}
	}
	return containerStatus{}
}

var tooLarge = Outcome{Reason: "镜像超过大小上限，构建被终止", Failure: "too_large"}

func (b *K8sJobBuilder) wait(ctx context.Context, name string, job BuildJob, token string, out io.Writer) (Outcome, error) {
	timedOut := Outcome{Reason: fmt.Sprintf("构建超时（%d 秒）", int(job.Timeout.Seconds())), Failure: "timed_out"}
	buildDeadline := time.Now().Add(job.Timeout)
	serveDeadline := buildDeadline.Add(b.cfg.FetchWindow)
	for {
		var j struct {
			Status struct {
				Conditions []struct {
					Type, Status, Reason string
				} `json:"conditions"`
			} `json:"status"`
		}
		if err := b.kube.JSON(ctx, http.MethodGet, b.path("jobs", name), nil, &j); err != nil {
			if providererr.CategoryOf(err) == providererr.NotFound {
				return Outcome{Reason: "构建的 Job 不见了", Failure: "failed"}, nil
			}
			return Outcome{}, err
		}
		for _, cond := range j.Status.Conditions {
			if cond.Type == "Failed" && cond.Status == "True" {
				if cond.Reason == "DeadlineExceeded" {
					return timedOut, nil
				}
				return b.failure(ctx, name, job, cond.Reason), nil
			}
		}
		p, err := b.pod(ctx, name)
		if err != nil {
			return Outcome{}, err
		}
		if p != nil && p.Status.Reason == "Evicted" {
			return tooLarge, nil
		}
		var build containerStatus
		if p != nil {
			build = find(p.Status.InitContainerStatuses, "build")
		}
		if t := build.State.Terminated; t != nil {
			if t.ExitCode != 0 {
				return b.failure(ctx, name, job, t.Reason), nil
			}
			serve := find(p.Status.ContainerStatuses, "serve")
			if serve.Ready && p.Status.PodIP != "" {
				return b.collect(ctx, p.Status.PodIP, token, out, job)
			}
			if time.Now().After(serveDeadline) {
				return Outcome{Reason: "构建完成了，但取不回产物（提供产物的容器没有就绪）", Failure: "failed"}, nil
			}
		} else if time.Now().After(buildDeadline) {
			return timedOut, nil
		}
		select {
		case <-ctx.Done():
			return Outcome{}, ctx.Err()
		case <-time.After(b.cfg.PollInterval):
		}
	}
}

func (b *K8sJobBuilder) collect(ctx context.Context, podIP, token string, out io.Writer, job BuildJob) (Outcome, error) {
	target := "http://" + net.JoinHostPort(podIP, strconv.Itoa(AgentPort)) + "/v1/files/" + artifact
	if _, err := b.fetch(ctx, target, token, out, job.MaxBytes); err != nil {
		var pe *providererr.Error
		if errors.As(err, &pe) && pe.Code == "image_too_large" {
			return tooLarge, nil
		}
		if errors.As(err, &pe) && pe.Category == providererr.Transient {
			return Outcome{Reason: pe.Message, Failure: "failed"}, nil
		}
		return Outcome{}, err
	}
	return Outcome{OK: true}, nil
}

func (b *K8sJobBuilder) failure(ctx context.Context, name string, job BuildJob, reason string) Outcome {
	tail := b.logs(ctx, name, job)
	if tail == "Evicted" || strings.Contains(strings.ToLower(tail), "ephemeral") {
		return tooLarge
	}
	if tail == "OOMKilled" {
		return Outcome{Reason: "构建用光了内存（OOMKilled）", Failure: "failed"}
	}
	if tail != "" {
		return Outcome{Reason: "构建失败：" + tail, Failure: "failed"}
	}
	if reason == "" {
		reason = "Failed"
	}
	return Outcome{Reason: "构建失败（" + reason + "）", Failure: "failed"}
}

// logs 是构建日志的最后几行（去掉地址与凭据），给用户看失败原因。
func (b *K8sJobBuilder) logs(ctx context.Context, name string, job BuildJob) string {
	p, err := b.pod(ctx, name)
	if err != nil || p == nil {
		return ""
	}
	if p.Status.Reason == "Evicted" {
		return "Evicted"
	}
	for _, cs := range append(append([]containerStatus{}, p.Status.InitContainerStatuses...), p.Status.ContainerStatuses...) {
		if t := cs.State.Terminated; t != nil && (t.Reason == "Evicted" || t.Reason == "OOMKilled") {
			return t.Reason
		}
	}
	status, data, err := b.kube.Request(ctx, http.MethodGet, b.path("pods", p.Metadata.Name)+"/log?container=build&tailLines=20", nil, "")
	if err != nil || status != 200 {
		return ""
	}
	text := []rune(Redact(decodeReplace(data), job))
	if len(text) > 1500 {
		text = text[len(text)-1500:]
	}
	return string(text)
}

var envCredential = regexp.MustCompile(`ar1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`)

// Redact 去掉日志里联网变量的值（里面有凭据）、环境凭据的样子，以及别家的签名地址这类看着像凭据的东西（Scrub）。在截取尾部之前做，
// 免得签名地址被截成认不出来的半截。
func Redact(text string, job BuildJob) string {
	values := make([]string, 0, len(job.Env))
	for _, v := range job.Env {
		values = append(values, v)
	}
	// 长的先换：一个值是另一个的前缀时不留半截。
	sort.Slice(values, func(i, j int) bool { return len(values[i]) > len(values[j]) })
	for _, v := range values {
		if len(v) >= 8 {
			text = strings.ReplaceAll(text, v, "***")
		}
	}
	return Scrub(envCredential.ReplaceAllString(text, "***"))
}

// HTTPFetch 从构建 Pod 里的 sandbox-agent 取回文件（不走代理；令牌放在 X-Sandbox-Token，不进日志与错误信息）。
func HTTPFetch(ctx context.Context, target, token string, out io.Writer, limit int64) (int64, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return 0, providererr.New(providererr.Transient, "builder_fetch_failed", "取不回构建产物（地址不对）")
	}
	req.Header.Set("X-Sandbox-Token", token)
	client := &http.Client{Timeout: 10 * time.Minute, Transport: &http.Transport{Proxy: nil}}
	resp, err := client.Do(req)
	if err != nil {
		return 0, providererr.New(providererr.Transient, "builder_fetch_failed", "取不回构建产物（连不上）")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return 0, providererr.New(providererr.Transient, "builder_fetch_failed", fmt.Sprintf("取不回构建产物（HTTP %d）", resp.StatusCode))
	}
	n, err := io.Copy(out, io.LimitReader(resp.Body, limit+1))
	if n > limit {
		return n, providererr.New(providererr.Invalid, "image_too_large", "构建产物超过大小上限")
	}
	if err != nil {
		return n, providererr.New(providererr.Transient, "builder_fetch_failed", "取不回构建产物（传输中断）")
	}
	return n, nil
}

// BuilderFromSpec 按后端登记里 environments.builder 一节（kind k8s）建 Builder。
func BuilderFromSpec(spec map[string]any, k Kube) (*K8sJobBuilder, error) {
	str := func(key string) string { v, _ := spec[key].(string); return v }
	num := func(key string) int {
		switch v := spec[key].(type) {
		case float64:
			return int(v)
		case json.Number:
			n, _ := v.Int64()
			return int(n)
		}
		return 0
	}
	cfg := BuilderConfig{AgentImage: str("agent_image"), Namespace: str("namespace"), RuntimeClass: str("runtime_class"), Isolation: str("isolation"),
		BuildkitImage: str("buildkit_image"), WorkspaceClaim: str("workspace_claim"), ImagePullPolicy: str("image_pull_policy"),
		CPUMillis: num("cpu_millis"), MemoryMB: num("memory_mb")}
	if _, ok := spec["isolation"]; ok && cfg.Isolation != "microvm" {
		return nil, errors.New("构建不可信的 Dockerfile 只能在 microVM 里：runtime_class 要是 Kata 一类、isolation 要是 microvm")
	}
	if _, ok := spec["runtime_class"]; ok && cfg.RuntimeClass == "" {
		return nil, errors.New("构建不可信的 Dockerfile 只能在 microVM 里：runtime_class 要是 Kata 一类、isolation 要是 microvm")
	}
	return NewK8sJobBuilder(k, cfg, nil)
}
