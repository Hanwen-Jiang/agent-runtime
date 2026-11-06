package k8ssandbox_test

// D-05：Sandbox 的 K8s 实现（每个环境一个一次性 Pod，agent 在 Pod 里执行；SBX-01 从 Python 的 tests/adapters/test_k8s_sandbox.py
// 搬来）。用 kind 拉起的集群（kindtest，经 API Server 的 pods/proxy 连 agent）。gVisor 设 AR_KIND_GVISOR；Kata 在 kind 里没有。
//
//   - 与本地实现同一套一致性测试，含绑定工作区的规则（工作区经 deploy/cluster/workspaces.yaml 的 PV、PVC 挂进沙箱）与存档。
//   - 集群约束（deploy/cluster/sandboxes.yaml）：沙箱以 10001 跑、根文件系统只读、没有 ServiceAccount 令牌、连不上外网与集群内服务。
//   - 令牌：agent 不接受别的令牌；令牌由 token_key 派生，换了 token_key 的进程连不上；命令在 /proc 里读不到 agent 的令牌。
//   - 空闲到期的 Pod 被回收；按域名放行的网络请求 invalid；显式镜像只收本租户路径下按 digest 写的。

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/k8ssandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/kube"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/kube/kindtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/sandboxtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

var (
	cluster    *kindtest.Cluster
	clusterErr error
)

func TestMain(m *testing.M) {
	cluster, clusterErr = kindtest.Start()
	code := m.Run()
	if cluster != nil {
		cluster.Stop()
	}
	os.Exit(code)
}

func needCluster(t *testing.T) *kindtest.Cluster {
	t.Helper()
	if clusterErr != nil {
		if errors.Is(clusterErr, kindtest.ErrMissing) && os.Getenv("AR_ALLOW_MISSING_TOOLS") == "1" {
			t.Skip(clusterErr)
		}
		t.Fatal(clusterErr)
	}
	return cluster
}

const timeout = 60 * time.Second

func makeProvider(t *testing.T, faults *testkit.Faults, tokenKey string, edit func(*k8ssandbox.Config)) (*k8ssandbox.Provider, *kube.API) {
	t.Helper()
	c := needCluster(t)
	api, err := kube.FromKubeconfig(c.Kubeconfig)
	if err != nil {
		t.Fatal(err)
	}
	cfg := k8ssandbox.Config{
		Namespace: "sandboxes", Images: map[string]string{"python": c.PythonImage}, AgentImage: c.AgentImage, Connect: "api_proxy",
		WorkspaceClaim: "workspaces", ImagePullPolicy: "IfNotPresent", StartupTimeout: 3 * time.Minute,
		// 设了 AR_KIND_GVISOR 时整套一致性测试都在 RuntimeClass gvisor 上跑。
		RuntimeClass: c.RuntimeClass,
	}
	if edit != nil {
		edit(&cfg)
	}
	if tokenKey == "" {
		tokenKey = strings.Repeat("k", 32)
	}
	p, err := k8ssandbox.New(api, cfg, []byte(tokenKey), faults)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { deleteAll(api) })
	return p, api
}

func deleteAll(api *kube.API) {
	sel := kube.LabelSelector(map[string]string{k8ssandbox.ManagedByLabel: k8ssandbox.ManagedBy})
	_, _, _ = api.Request(context.Background(), "DELETE", "/api/v1/namespaces/sandboxes/pods?gracePeriodSeconds=0&labelSelector="+sel, nil, "")
}

type harness struct {
	p      *k8ssandbox.Provider
	faults *testkit.Faults
	c      *kindtest.Cluster
}

func (h *harness) Provider() sandbox.Provider { return h.p }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}
func (h *harness) WorkspacePath(tenant, ws string) string { return h.c.WorkspacePath(tenant, ws) }

func TestK8sPodSandboxConformance(t *testing.T) {
	needCluster(t)
	sandboxtest.Run(t, func(t *testing.T) sandboxtest.Harness {
		faults := testkit.NewFaults()
		p, _ := makeProvider(t, faults, "", nil)
		return &harness{p: p, faults: faults, c: cluster}
	})
}

func run(t *testing.T, p *k8ssandbox.Provider, code string) (int, string, string) {
	t.Helper()
	ctx := context.Background()
	box := conformance.Must(p.Bind(conformance.TenantA()))
	env := conformance.Must(box.Create(ctx, sandbox.Spec{}))
	res := conformance.Must(box.Exec(ctx, env.ID, []string{"python", "-c", code}, sandbox.ExecOptions{Timeout: timeout}))
	exit := -1
	if res.ExitCode != nil {
		exit = *res.ExitCode
	}
	return exit, strings.TrimSpace(string(res.Stdout)), string(res.Stderr)
}

func TestSandboxRunsAsNonRootWithAReadOnlyRoot(t *testing.T) {
	p, _ := makeProvider(t, nil, "", nil)
	if _, out, _ := run(t, p, "import os; print(os.getuid())"); out != "10001" {
		t.Fatalf("uid = %q", out)
	}
	code, _, stderr := run(t, p, "open('/etc/x', 'w')")
	if code == 0 || !(strings.Contains(stderr, "Read-only file system") || strings.Contains(stderr, "Permission denied")) {
		t.Fatalf("写根文件系统：%d %s", code, stderr)
	}
}

func TestExecReportsCPUTimeForMetering(t *testing.T) {
	// D-10：agent 报出命令用掉的 CPU 时间，Sandbox Service 记进环境表与 span。
	p, _ := makeProvider(t, nil, "", nil)
	ctx := context.Background()
	box := conformance.Must(p.Bind(conformance.TenantA()))
	env := conformance.Must(box.Create(ctx, sandbox.Spec{}))
	res := conformance.Must(box.Exec(ctx, env.ID, []string{"python", "-c", "import time\nend = time.time() + 0.5\nwhile time.time() < end: pass"}, sandbox.ExecOptions{Timeout: timeout}))
	if res.CPUTime < 200*time.Millisecond {
		t.Fatalf("cpu = %v", res.CPUTime)
	}
}

func TestEachTenantHasItsOwnQuota(t *testing.T) {
	// D-10：一个租户同时最多 MaxEnvsPerScope 个环境，超了 denied；别的租户不受影响，销毁后又能建。
	p, _ := makeProvider(t, nil, "", func(c *k8ssandbox.Config) { c.MaxEnvsPerScope = 1 })
	ctx := context.Background()
	a := conformance.Must(p.Bind(conformance.TenantA()))
	b := conformance.Must(p.Bind(conformance.TenantB()))
	first := conformance.Must(a.Create(ctx, sandbox.Spec{}))
	_, err := a.Create(ctx, sandbox.Spec{})
	conformance.ExpectError(t, err, providererr.Denied, "sandbox_quota_exceeded")
	other := conformance.Must(b.Create(ctx, sandbox.Spec{}))
	conformance.NoError(t, a.Destroy(ctx, first.ID))
	conformance.Must(a.Create(ctx, sandbox.Spec{}))
	conformance.NoError(t, b.Destroy(ctx, other.ID))
}

func listPods(t *testing.T, api *kube.API) []map[string]any {
	t.Helper()
	var out struct {
		Items []map[string]any `json:"items"`
	}
	sel := kube.LabelSelector(map[string]string{k8ssandbox.ManagedByLabel: k8ssandbox.ManagedBy})
	conformance.NoError(t, api.JSON(context.Background(), "GET", "/api/v1/namespaces/sandboxes/pods?labelSelector="+sel, nil, &out))
	return out.Items
}

func TestGVisorRuntimeClassRunsUnderTheGVisorKernel(t *testing.T) {
	// D-05 验收 1（gVisor 那一半）：RuntimeClass gvisor 的沙箱里是 gVisor 的用户态内核。只认 gvisor 字样（版本号会变）。
	c := needCluster(t)
	if c.RuntimeClass != "gvisor" {
		t.Skip("设 AR_KIND_GVISOR（gVisor 的发布包）才在 kind 里装 gVisor")
	}
	p, api := makeProvider(t, nil, "", nil)
	if _, version, _ := run(t, p, "print(open('/proc/version').read())"); !strings.Contains(strings.ToLower(version), "gvisor") {
		t.Fatal(version)
	}
	pods := listPods(t, api)
	for _, pd := range pods {
		if pd["spec"].(map[string]any)["runtimeClassName"] != "gvisor" {
			t.Fatal("沙箱 Pod 没用 gvisor")
		}
	}
}

func TestSandboxHasNoServiceAccountToken(t *testing.T) {
	p, _ := makeProvider(t, nil, "", nil)
	if _, out, _ := run(t, p, "import os; print(os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'))"); out != "False" {
		t.Fatal(out)
	}
}

func TestSandboxCannotReachTheNetwork(t *testing.T) {
	p, _ := makeProvider(t, nil, "", nil)
	probe := "import socket\n" +
		"for host, port in [('1.1.1.1', 443), ('kubernetes.default.svc.cluster.local', 443), ('10.96.0.1', 443)]:\n" +
		"    try:\n" +
		"        socket.create_connection((host, port), timeout=3).close(); print('open', host)\n" +
		"    except OSError:\n" +
		"        print('blocked', host)\n"
	if _, out, _ := run(t, p, probe); strings.Contains(out, "open") {
		t.Fatal(out)
	}
}

func TestAgentOnlyAcceptsItsOwnToken(t *testing.T) {
	p, _ := makeProvider(t, nil, "", nil)
	ctx := context.Background()
	env := conformance.Must(conformance.Must(p.Bind(conformance.TenantA())).Create(ctx, sandbox.Spec{}))
	other, _ := makeProvider(t, nil, strings.Repeat("x", 32), nil)
	_, err := conformance.Must(other.Bind(conformance.TenantA())).Exec(ctx, env.ID, []string{"python", "-c", "print(1)"}, sandbox.ExecOptions{Timeout: timeout})
	conformance.ExpectError(t, err, providererr.Denied, "")
}

func TestCommandsCannotReadTheAgentTokenFromProc(t *testing.T) {
	// AGENT-TOK-01 / SBX-ZOMBIE：Rust 版由 PID 1 回收孤儿，子 agent 执行命令。
	// reaper 与 agent 都可能持有令牌，两者的 environ、mem 均须不可读。
	p, _ := makeProvider(t, nil, "", nil)
	ctx := context.Background()
	box := conformance.Must(p.Bind(conformance.TenantA()))
	env := conformance.Must(box.Create(ctx, sandbox.Spec{}))
	probe := "import json, os\n" +
		"found = {}\n" +
		"for pid in [p for p in os.listdir('/proc') if p.isdigit()]:\n" +
		"    for name in ('environ', 'cmdline'):\n" +
		"        try:\n" +
		"            found[pid + '/' + name] = open(f'/proc/{pid}/{name}', 'rb').read().decode('latin-1')\n" +
		"        except OSError:\n" +
		"            found[pid + '/' + name] = None\n" +
		"parent = os.getppid()\n" +
		"parent_parent = int(open(f'/proc/{parent}/stat').read().rsplit(')', 1)[1].split()[1])\n" +
		"parent_comm = open(f'/proc/{parent}/comm').read().strip()\n" +
		"mem = {}\n" +
		"for pid in {1, parent}:\n" +
		"    try:\n" +
		"        open(f'/proc/{pid}/mem', 'rb').close(); mem[str(pid)] = True\n" +
		"    except OSError:\n" +
		"        mem[str(pid)] = False\n" +
		"print(json.dumps({'ppid': parent, 'parent_parent': parent_parent, 'parent_comm': parent_comm, 'files': found, 'mem': mem}))\n"
	res := conformance.Must(box.Exec(ctx, env.ID, []string{"python", "-c", probe}, sandbox.ExecOptions{Timeout: timeout}))
	var seen struct {
		PPID         int                `json:"ppid"`
		ParentParent int                `json:"parent_parent"`
		ParentComm   string             `json:"parent_comm"`
		Files        map[string]*string `json:"files"`
		Mem          map[string]bool    `json:"mem"`
	}
	if err := json.Unmarshal(res.Stdout, &seen); err != nil {
		t.Fatalf("%v：%s %s", err, res.Stdout, res.Stderr)
	}
	token := p.Token(env.ID)
	if seen.PPID < 1 || seen.ParentComm != "sandbox-agent" || (seen.PPID != 1 && seen.ParentParent != 1) {
		t.Fatalf("命令必须由 agent 直接执行，agent 为 PID 1 或 PID 1 的子进程：ppid=%d, parent_parent=%d, comm=%q", seen.PPID, seen.ParentParent, seen.ParentComm)
	}
	for k, v := range seen.Files {
		if v != nil && strings.Contains(*v, token) {
			t.Fatalf("命令在 /proc 里读到了 agent 的令牌：%s", k)
		}
	}
	for _, pid := range []int{1, seen.PPID} {
		name := fmt.Sprint(pid)
		if value, present := seen.Files[name+"/environ"]; !present || value != nil {
			t.Fatalf("agent/reaper 的 environ 不该让命令读：pid=%d", pid)
		}
		if readable, present := seen.Mem[name]; !present || readable {
			t.Fatalf("命令能打开 agent/reaper 的 mem 或未验证它：pid=%d", pid)
		}
	}
}

func TestNetworkAllowlistNeedsAMicroVMBackend(t *testing.T) {
	p, _ := makeProvider(t, nil, "", nil)
	_, err := conformance.Must(p.Bind(conformance.TenantA())).Create(context.Background(), sandbox.Spec{Network: "allowlist", AllowHosts: []string{"pypi.org"}})
	conformance.ExpectError(t, err, providererr.Invalid, "network_allowlist_unsupported")
}

func TestExplicitImagesOnlyFromTheOwnTenantPathByDigest(t *testing.T) {
	// D-17：项目环境的镜像只能是环境镜像仓库里、本租户路径下按 digest 写的；别的写法在建 Pod 之前就拒绝。
	ctx := context.Background()
	digest := "sha256:" + strings.Repeat("0", 64)
	p, _ := makeProvider(t, nil, "", nil)
	_, err := conformance.Must(p.Bind(conformance.TenantA())).Create(ctx, sandbox.Spec{Image: fmt.Sprintf("127.0.0.1:30500/envs/%s/env@%s", conformance.TenantAID, digest)})
	conformance.ExpectError(t, err, providererr.Invalid, "unsupported_image")
	scoped, api := makeProvider(t, nil, "", func(c *k8ssandbox.Config) { c.CustomImagePrefix = "127.0.0.1:30500/envs" })
	a := conformance.Must(scoped.Bind(conformance.TenantA()))
	for _, image := range []string{
		fmt.Sprintf("127.0.0.1:30500/envs/%s/env@%s", conformance.TenantBID, digest),  // 别的租户的路径
		fmt.Sprintf("127.0.0.1:30500/envs/%s/env:latest", conformance.TenantAID),      // 按标签
		fmt.Sprintf("evil.example.com/envs/%s/env@%s", conformance.TenantAID, digest), // 别的仓库
		"python:3.13-slim",
	} {
		_, err := a.Create(ctx, sandbox.Spec{Image: image})
		conformance.ExpectError(t, err, providererr.Invalid, "unsupported_image")
	}
	_, err = conformance.Must(scoped.BindPlatform("check")).Create(ctx, sandbox.Spec{Image: "127.0.0.1:30500/envs/_platform/env@" + digest})
	conformance.ExpectError(t, err, providererr.Invalid, "unsupported_image")
	if pods := listPods(t, api); len(pods) != 0 {
		t.Fatalf("拒绝的都没有建 Pod：%d", len(pods))
	}
}

func TestIdleEnvironmentsAreReaped(t *testing.T) {
	p, _ := makeProvider(t, nil, "", nil)
	ctx := context.Background()
	box := conformance.Must(p.Bind(conformance.TenantA()))
	env := conformance.Must(box.Create(ctx, sandbox.Spec{IdleTimeout: time.Second}))
	time.Sleep(2 * time.Second)
	if n := conformance.Must(p.Reap(ctx, true)); n < 1 {
		t.Fatalf("reaped = %d", n)
	}
	_, err := box.Exec(ctx, env.ID, []string{"python", "-c", "print(1)"}, sandbox.ExecOptions{Timeout: timeout})
	conformance.ExpectError(t, err, providererr.NotFound, "")
}

func TestPodSpecIsLockedDown(t *testing.T) {
	p, api := makeProvider(t, nil, "", nil)
	ctx := context.Background()
	env := conformance.Must(conformance.Must(p.Bind(conformance.TenantA())).Create(ctx, sandbox.Spec{CPUMillis: 500, MemoryMB: 256}))
	var pd struct {
		Metadata struct {
			Labels map[string]string `json:"labels"`
		} `json:"metadata"`
		Spec struct {
			AutomountServiceAccountToken *bool `json:"automountServiceAccountToken"`
			EnableServiceLinks           *bool `json:"enableServiceLinks"`
			Containers                   []struct {
				SecurityContext struct {
					ReadOnlyRootFilesystem bool `json:"readOnlyRootFilesystem"`
					Capabilities           struct {
						Drop []string `json:"drop"`
					} `json:"capabilities"`
				} `json:"securityContext"`
				Resources struct {
					Limits map[string]string `json:"limits"`
				} `json:"resources"`
				Env []struct {
					Name  string `json:"name"`
					Value string `json:"value"`
				} `json:"env"`
			} `json:"containers"`
		} `json:"spec"`
	}
	conformance.NoError(t, api.JSON(ctx, "GET", "/api/v1/namespaces/sandboxes/pods/"+env.ID, nil, &pd))
	if pd.Spec.AutomountServiceAccountToken == nil || *pd.Spec.AutomountServiceAccountToken || pd.Spec.EnableServiceLinks == nil || *pd.Spec.EnableServiceLinks {
		t.Fatal("不挂 ServiceAccount 令牌、不注入 Service 的环境变量")
	}
	c := pd.Spec.Containers[0]
	if !c.SecurityContext.ReadOnlyRootFilesystem || len(c.SecurityContext.Capabilities.Drop) != 1 || c.SecurityContext.Capabilities.Drop[0] != "ALL" {
		t.Fatalf("securityContext = %+v", c.SecurityContext)
	}
	if c.Resources.Limits["memory"] != "256Mi" || pd.Metadata.Labels[k8ssandbox.NetworkLabel] != "none" {
		t.Fatalf("limits = %v labels = %v", c.Resources.Limits, pd.Metadata.Labels)
	}
	token := ""
	for _, e := range c.Env {
		if e.Name == "AGENT_TOKEN" {
			token = e.Value
		}
	}
	if token != p.Token(env.ID) {
		t.Fatal("AGENT_TOKEN 是由 token_key 派生的")
	}
	labels, _ := json.Marshal(pd.Metadata.Labels)
	if strings.Contains(string(labels), conformance.TenantAID) {
		t.Fatal("标签里只有作用域的哈希，不直接写租户 ID")
	}
}
