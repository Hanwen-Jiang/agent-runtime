// Package kindtest 在测试里用 kind 拉起（或复用）一个 Docker 里的小 K8s 集群，给 K8s 的 Sandbox 适配器与项目环境的构建跑测试
// （D-05、D-17；SBX-01 从 Python 的 agentruntime_core.adapters.kindtest 搬来，做法不变）。
//
//   - kind 取自 AR_KIND_BIN 或 PATH；docker 与 kubectl 取自 AR_DOCKER_BIN 指定的目录或 PATH。不让测试自行安装。找不到、或 Docker
//     没在运行时测试失败；设 AR_ALLOW_MISSING_TOOLS=1 改为跳过（Missing）。
//   - 集群名 agentruntime-test。默认用完删掉；设 AR_KIND_REUSE=1 时已有就复用、用完留着。
//   - 工作区：宿主目录（AR_KIND_WORKSPACES，默认 ~/.local/kindtest/workspaces）经 kind 的 extraMounts 挂到节点的
//     /var/lib/agentruntime/workspaces，再按 deploy/cluster/workspaces.yaml 建 PV 与 PVC，同生产的单节点部署。
//   - 应用 deploy/cluster 的 namespaces.yaml、sandboxes.yaml、workspaces.yaml。
//   - 镜像：sandbox-agent 用本仓库的源码编译成 Linux 静态二进制，放进 FROM scratch 的镜像；跑命令的镜像默认 python:3.13-slim
//     （本机没有时 docker pull）。都用 kind load 装进节点。AR_SANDBOX_AGENT_IMAGE 给了就用现成的 agent 镜像。
//   - gVisor：设 AR_KIND_GVISOR 为 gVisor 发布包（旁边要有同名的 .sha512）时装进节点、登记 runsc、应用 runtimeclasses.yaml；
//     之后沙箱 Pod 用 RuntimeClass gvisor（Cluster.RuntimeClass）。Kata 要嵌套虚拟化，kind 里没有。
package kindtest

import (
	"crypto/sha512"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"time"
)

const (
	ClusterName        = "agentruntime-test"
	NodeWorkspaces     = "/var/lib/agentruntime/workspaces"
	PythonImage        = "python:3.13-slim"
	SandboxPythonImage = "agentruntime/sandbox-python:test"
	AgentImage         = "agentruntime/sandbox-agent:test"
)

// ErrMissing 表示缺工具或 Docker 没在运行；AR_ALLOW_MISSING_TOOLS=1 时调用方跳过。
var ErrMissing = errors.New("kindtest: 缺工具")

// Cluster 是一个可用的 kind 集群。
type Cluster struct {
	Docker, Kubectl, Kind string
	// Kubeconfig 是 JSON 格式的 kubeconfig（kube.FromKubeconfig 读它）；KubeconfigYAML 给 kubectl。
	Kubeconfig     string
	KubeconfigYAML string
	Workspaces     string
	AgentImage     string
	PythonImage    string
	RuntimeClass   string

	created bool
	reuse   bool
	dir     string
	env     []string
}

// RepoRoot 是仓库根。
func RepoRoot() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(file), "..", "..", "..", "..", "..")
}

func tool(name, envDir string) (string, error) {
	if envDir != "" {
		dir := envDir
		if st, err := os.Stat(envDir); err == nil && !st.IsDir() {
			dir = filepath.Dir(envDir)
		}
		if p := filepath.Join(dir, name); fileExists(p) {
			return p, nil
		}
		if fileExists(envDir) && filepath.Base(envDir) == name {
			return envDir, nil
		}
	}
	p, err := exec.LookPath(name)
	if err != nil {
		return "", fmt.Errorf("%w：找不到 %s（K8s 沙箱的测试用 kind 拉起集群），见 CLAUDE.md 的常用命令", ErrMissing, name)
	}
	return p, nil
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && !st.IsDir()
}

// Start 拉起或复用集群；出错时已建的东西收掉。
func Start() (*Cluster, error) {
	c := &Cluster{}
	var err error
	dockerDir := os.Getenv("AR_DOCKER_BIN")
	if c.Docker, err = tool("docker", dockerDir); err != nil {
		return nil, err
	}
	kubectlDir := dockerDir
	if kubectlDir == "" {
		kubectlDir = filepath.Dir(c.Docker)
	}
	if c.Kubectl, err = tool("kubectl", kubectlDir); err != nil {
		return nil, err
	}
	if c.Kind, err = tool("kind", os.Getenv("AR_KIND_BIN")); err != nil {
		return nil, err
	}
	c.env = append(os.Environ(), "PATH="+strings.Join([]string{filepath.Dir(c.Docker), filepath.Dir(c.Kind), os.Getenv("PATH")}, string(os.PathListSeparator)))
	if _, err := c.run(30*time.Second, "", c.Docker, "info", "--format", "{{.ServerVersion}}"); err != nil {
		return nil, fmt.Errorf("%w：Docker 没在运行（docker info 失败）", ErrMissing)
	}
	c.reuse = os.Getenv("AR_KIND_REUSE") == "1"
	c.Workspaces = os.Getenv("AR_KIND_WORKSPACES")
	if c.Workspaces == "" {
		home, _ := os.UserHomeDir()
		c.Workspaces = filepath.Join(home, ".local", "kindtest", "workspaces")
	}
	if err := os.MkdirAll(c.Workspaces, 0o777); err != nil {
		return nil, err
	}
	// 沙箱以 10001 跑：工作区根要让它写得进去（同生产的 install.sh）。
	_ = os.Chmod(c.Workspaces, 0o777)
	if c.dir, err = os.MkdirTemp("", "ar-kind-"); err != nil {
		return nil, err
	}
	if err := c.setup(); err != nil {
		c.Stop()
		return nil, err
	}
	return c, nil
}

func (c *Cluster) setup() error {
	out, _ := c.run(time.Minute, "", c.Kind, "get", "clusters")
	c.created = !slices.Contains(strings.Fields(out), ClusterName)
	if c.created {
		if err := c.create(); err != nil {
			return err
		}
	} else if !c.reuse {
		return fmt.Errorf("已经有一个叫 %s 的 kind 集群：删掉它（kind delete cluster --name %s）或设 AR_KIND_REUSE=1 复用", ClusterName, ClusterName)
	}
	if err := c.kubeconfig(); err != nil {
		return err
	}
	if pkg := os.Getenv("AR_KIND_GVISOR"); pkg != "" {
		if err := c.installGVisor(pkg); err != nil {
			return err
		}
		c.RuntimeClass = "gvisor"
	}
	c.AgentImage = os.Getenv("AR_SANDBOX_AGENT_IMAGE")
	if c.AgentImage == "" {
		if err := c.buildAgent(); err != nil {
			return err
		}
	}
	if err := c.pythonImage(); err != nil {
		return err
	}
	if _, err := c.run(15*time.Minute, "", c.Kind, "load", "docker-image", c.AgentImage, c.PythonImage, "--name", ClusterName); err != nil {
		return err
	}
	for _, m := range []string{"namespaces.yaml", "sandboxes.yaml", "workspaces.yaml"} {
		if _, err := c.KubectlRun("apply", "-f", filepath.Join(RepoRoot(), "deploy", "cluster", m)); err != nil {
			return err
		}
	}
	return c.waitBound()
}

func (c *Cluster) run(timeout time.Duration, stdin, name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	cmd.Env = c.env
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	var stdout, stderr strings.Builder
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Start(); err != nil {
		return "", err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		if err != nil {
			msg := strings.TrimSpace(stderr.String() + stdout.String())
			if len(msg) > 2000 {
				msg = msg[len(msg)-2000:]
			}
			return stdout.String(), fmt.Errorf("%s %s 失败：%s", filepath.Base(name), strings.Join(args[:min(2, len(args))], " "), msg)
		}
		return stdout.String(), nil
	case <-time.After(timeout):
		_ = cmd.Process.Kill()
		return "", fmt.Errorf("%s %s 超时", filepath.Base(name), strings.Join(args[:min(2, len(args))], " "))
	}
}

// KubectlRun 用这个集群的 kubeconfig 跑 kubectl。
func (c *Cluster) KubectlRun(args ...string) (string, error) {
	return c.run(5*time.Minute, "", c.Kubectl, append([]string{"--kubeconfig", c.KubeconfigYAML}, args...)...)
}

func (c *Cluster) create() error {
	cfg := map[string]any{
		"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4",
		"nodes": []any{map[string]any{"role": "control-plane", "extraMounts": []any{map[string]any{"hostPath": c.Workspaces, "containerPath": NodeWorkspaces}}}},
	}
	path := filepath.Join(c.dir, "kind.json")
	data, _ := json.Marshal(cfg)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return err
	}
	args := []string{"create", "cluster", "--name", ClusterName, "--config", path, "--wait", "120s"}
	if img := os.Getenv("AR_KIND_NODE_IMAGE"); img != "" {
		args = append(args, "--image", img)
	}
	_, err := c.run(20*time.Minute, "", c.Kind, args...)
	return err
}

func (c *Cluster) kubeconfig() error {
	raw, err := c.run(time.Minute, "", c.Kind, "get", "kubeconfig", "--name", ClusterName)
	if err != nil {
		return err
	}
	c.KubeconfigYAML = filepath.Join(c.dir, "kubeconfig.yaml")
	if err := os.WriteFile(c.KubeconfigYAML, []byte(raw), 0o600); err != nil {
		return err
	}
	data, err := c.KubectlRun("config", "view", "--raw", "--minify", "-o", "json")
	if err != nil {
		return err
	}
	c.Kubeconfig = filepath.Join(c.dir, "kubeconfig.json")
	return os.WriteFile(c.Kubeconfig, []byte(data), 0o600)
}

func (c *Cluster) buildAgent() error {
	build := filepath.Join(c.dir, "agent")
	if err := os.MkdirAll(build, 0o755); err != nil {
		return err
	}
	cmd := exec.Command("go", "build", "-trimpath", "-o", filepath.Join(build, "sandbox-agent"), "./cmd/sandbox-agent")
	cmd.Dir = filepath.Join(RepoRoot(), "services", "go-sandbox-agent")
	cmd.Env = append(os.Environ(), "GOOS=linux", "GOARCH=amd64", "CGO_ENABLED=0")
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("编译 sandbox-agent 失败：%v\n%s", err, out)
	}
	dockerfile := "FROM scratch\nCOPY sandbox-agent /sandbox-agent\nUSER 10001\nENTRYPOINT [\"/sandbox-agent\"]\n"
	if err := os.WriteFile(filepath.Join(build, "Dockerfile"), []byte(dockerfile), 0o644); err != nil {
		return err
	}
	if _, err := c.run(10*time.Minute, "", c.Docker, "build", "-q", "-t", AgentImage, build); err != nil {
		return err
	}
	c.AgentImage = AgentImage
	return nil
}

// pythonImage：Docker Desktop 拉下来的是多平台镜像，kind load 会找不到别的平台的层；在它上面构建一层，得到只有本平台的镜像。
func (c *Cluster) pythonImage() error {
	if _, err := c.run(time.Minute, "", c.Docker, "image", "inspect", PythonImage); err != nil {
		if _, err := c.run(15*time.Minute, "", c.Docker, "pull", PythonImage); err != nil {
			return err
		}
	}
	build := filepath.Join(c.dir, "python")
	if err := os.MkdirAll(build, 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(build, "Dockerfile"), []byte("FROM "+PythonImage+"\n"), 0o644); err != nil {
		return err
	}
	if _, err := c.run(10*time.Minute, "", c.Docker, "build", "-q", "-t", SandboxPythonImage, build); err != nil {
		return err
	}
	c.PythonImage = SandboxPythonImage
	return nil
}

// installGVisor 把 gVisor 装进 kind 的节点并给 containerd 登记 runsc（RuntimeClass gvisor 的 handler）。重复调用是安全的。
func (c *Cluster) installGVisor(pkg string) error {
	sums := pkg + ".sha512"
	if !fileExists(pkg) || !fileExists(sums) {
		return fmt.Errorf("%w：AR_KIND_GVISOR 要指向 gVisor 的发布包，旁边要有 %s", ErrMissing, filepath.Base(sums))
	}
	wantRaw, _ := os.ReadFile(sums)
	want := strings.ToLower(strings.Fields(string(wantRaw))[0])
	f, err := os.Open(pkg)
	if err != nil {
		return err
	}
	h := sha512.New()
	_, err = io.Copy(h, f)
	f.Close()
	if err != nil {
		return err
	}
	if hex.EncodeToString(h.Sum(nil)) != want {
		return fmt.Errorf("%s 的 sha512 对不上", filepath.Base(pkg))
	}
	node := ClusterName + "-control-plane"
	if _, err := c.run(time.Minute, "", c.Docker, "exec", node, "sh", "-c", "test -x /usr/local/bin/runsc && test -d /usr/local/bin/gvisor-bin"); err != nil {
		out := filepath.Join(c.dir, "gvisor")
		if err := os.MkdirAll(out, 0o755); err != nil {
			return err
		}
		if _, err := c.run(10*time.Minute, "", "tar", "-xf", pkg, "-C", out); err != nil {
			return err
		}
		items, _ := os.ReadDir(out)
		for _, it := range items {
			if _, err := c.run(10*time.Minute, "", c.Docker, "cp", filepath.Join(out, it.Name()), node+":/usr/local/bin/"); err != nil {
				return err
			}
		}
		if _, err := c.run(time.Minute, "", c.Docker, "exec", node, "sh", "-c",
			"chmod 0755 /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1 && chmod -R a+rX /usr/local/bin/gvisor-bin "+
				"&& find /usr/local/bin/gvisor-bin -type f -exec chmod a+x {} +"); err != nil {
			return err
		}
	}
	config, err := c.run(time.Minute, "", c.Docker, "exec", node, "cat", "/etc/containerd/config.toml")
	if err != nil {
		return err
	}
	if !strings.Contains(config, "runtimes.runsc]") {
		block := "\n[plugins.\"io.containerd.grpc.v1.cri\".containerd.runtimes.runsc]\n  runtime_type = \"io.containerd.runsc.v1\"\n"
		if _, err := c.run(time.Minute, block, c.Docker, "exec", "-i", node, "sh", "-c", "cat >> /etc/containerd/config.toml"); err != nil {
			return err
		}
		if _, err := c.run(time.Minute, "", c.Docker, "exec", node, "systemctl", "restart", "containerd"); err != nil {
			return err
		}
		if _, err := c.KubectlRun("wait", "--for=condition=Ready", "node", "--all", "--timeout=180s"); err != nil {
			return err
		}
	}
	_, err = c.KubectlRun("apply", "-f", filepath.Join(RepoRoot(), "deploy", "cluster", "runtimeclasses.yaml"))
	return err
}

func (c *Cluster) waitBound() error {
	deadline := time.Now().Add(60 * time.Second)
	for {
		phase, _ := c.KubectlRun("get", "pvc", "workspaces", "-n", "sandboxes", "-o", "jsonpath={.status.phase}")
		if strings.TrimSpace(phase) == "Bound" {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("sandboxes/workspaces 没有绑定（%s）", phase)
		}
		time.Sleep(time.Second)
	}
}

// WorkspacePath 是宿主一侧看到的工作区目录。
func (c *Cluster) WorkspacePath(tenant, workspace string) string {
	return filepath.Join(c.Workspaces, tenant, workspace)
}

// Stop 删掉这次建的集群（AR_KIND_REUSE=1 时留着）。
func (c *Cluster) Stop() {
	if c.created && !c.reuse {
		_, _ = c.run(5*time.Minute, "", c.Kind, "delete", "cluster", "--name", ClusterName)
	}
	if c.dir != "" {
		_ = os.RemoveAll(c.dir)
	}
}
