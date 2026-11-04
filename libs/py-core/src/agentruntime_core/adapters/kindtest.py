"""测试里用 kind 拉起（或复用）一个 Docker 里的小 K8s 集群，给 K8s 的 Sandbox 适配器跑一致性测试（docs/plan/cloud-deploy.md D-05）。

- kind 取自 AR_KIND_BIN 或 PATH；docker 与 kubectl 取自 AR_DOCKER_BIN 指定的目录或 PATH（Docker Desktop 的 resources\\bin 里两个都有）。
  不让测试自行安装。找不到、或 Docker 没在运行时测试失败；设 AR_ALLOW_MISSING_TOOLS=1 改为跳过。
- 集群名 agentruntime-test。默认用完删掉；设 AR_KIND_REUSE=1 时已有就复用、用完留着（反复跑测试时省掉一两分钟的建集群）。
- 工作区：宿主目录（AR_KIND_WORKSPACES，默认 ~/.local/kindtest/workspaces，要在 Docker 能挂载的本地盘上）经 kind 的
  extraMounts 挂到节点的 /var/lib/agentruntime/workspaces，再按 deploy/cluster/workspaces.yaml 建 PV 与 PVC，同生产的单节点部署。
- 应用 deploy/cluster 的 namespaces.yaml、sandboxes.yaml、workspaces.yaml（沙箱命名空间的 Pod Security、默认拒绝的网络策略、配额）。
- 镜像：sandbox-agent 用本仓库的源码编译成 Linux 静态二进制（默认是 Rust 版，musl；见 agentbuild），放进 FROM scratch 的镜像（不用拉基础镜像）；跑命令的镜像默认
  python:3.13-slim（本机没有时 docker pull）。都用 kind load 装进节点。AR_SANDBOX_AGENT_IMAGE 给了就用现成的 agent 镜像。
- gVisor：设 AR_KIND_GVISOR 为 gVisor 发布包（gvisor.tar.zstd 或 .tar.bz2，旁边要有同名的 .sha512）时，核对 sha512、在宿主上
  解开，把 runsc、containerd-shim-runsc-v1 与 gvisor-bin/ 拷进节点，给节点的 containerd 登记 runsc，再应用
  deploy/cluster/runtimeclasses.yaml；之后沙箱 Pod 用 RuntimeClass gvisor（Cluster.runtime_class）。不让测试自行下载。
  Kata 要嵌套虚拟化，kind 里没有，留到真机器上验（D-02 的验收）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from agentruntime_core.adapters import agentbuild

CLUSTER = "agentruntime-test"
NODE_WORKSPACES = "/var/lib/agentruntime/workspaces"
PYTHON_IMAGE = "python:3.13-slim"
SANDBOX_PYTHON_IMAGE = "agentruntime/sandbox-python:test"
AGENT_IMAGE = "agentruntime/sandbox-agent:test"
REPO_ROOT = Path(__file__).resolve().parents[5]


class MissingTool(RuntimeError):
    pass


def _tool(name: str, env_dir: str = "") -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    if env_dir:
        path = Path(env_dir)
        candidate = path / exe if path.is_dir() else path.parent / exe
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found is None:
        raise MissingTool(f"找不到 {name}（K8s 沙箱的测试用 kind 拉起集群），见 CLAUDE.md 的常用命令")
    return found


class Cluster:
    def __init__(self) -> None:
        docker_dir = os.environ.get("AR_DOCKER_BIN", "")
        self.docker = _tool("docker", docker_dir)
        self.kubectl = _tool("kubectl", docker_dir or str(Path(self.docker).parent))
        self.kind = _tool("kind", os.environ.get("AR_KIND_BIN", ""))
        self._env = {**os.environ, "PATH": os.pathsep.join([str(Path(self.docker).parent), str(Path(self.kind).parent), os.environ.get("PATH", "")])}
        if self._run(self.docker, "info", "--format", "{{.ServerVersion}}", check=False).returncode != 0:
            raise MissingTool("Docker 没在运行（docker info 失败），先启动 Docker Desktop")
        self.reuse = os.environ.get("AR_KIND_REUSE") == "1"
        self.workspaces = Path(os.environ.get("AR_KIND_WORKSPACES") or Path.home() / ".local" / "kindtest" / "workspaces")
        self.workspaces.mkdir(parents=True, exist_ok=True)
        self._dir = Path(tempfile.mkdtemp(prefix="ar-kind-"))
        try:
            existing = self._run(self.kind, "get", "clusters", check=False).stdout.split()
            self.created = CLUSTER not in existing
            if self.created:
                self._create()
            elif not self.reuse:
                raise RuntimeError(f"已经有一个叫 {CLUSTER} 的 kind 集群：删掉它（kind delete cluster --name {CLUSTER}）或设 AR_KIND_REUSE=1 复用")
            self.kubeconfig = self._kubeconfig()
            self.runtime_class = ""
            if os.environ.get("AR_KIND_GVISOR"):
                self._install_gvisor(Path(os.environ["AR_KIND_GVISOR"]))
                self.runtime_class = "gvisor"
            self.agent_image = os.environ.get("AR_SANDBOX_AGENT_IMAGE") or self._build_agent()
            self.python_image = self._python_image()
            self._run(self.kind, "load", "docker-image", self.agent_image, self.python_image, "--name", CLUSTER, timeout=900)
            for manifest in ("namespaces.yaml", "sandboxes.yaml", "workspaces.yaml"):
                self.kubectl_run("apply", "-f", str(REPO_ROOT / "deploy" / "cluster" / manifest))
            self._wait_bound()
        except BaseException:
            self.stop()
            raise

    def _run(self, *args: str, check: bool = True, timeout: float = 300, input: str | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            list(args), env=self._env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, input=input
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"{Path(args[0]).name} {' '.join(args[1:3])} 失败：{(result.stderr or result.stdout).strip()[-2000:]}")
        return result

    def kubectl_run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._run(self.kubectl, "--kubeconfig", str(self._dir / "kubeconfig.yaml"), *args, check=check)

    def _create(self) -> None:
        config = {
            "kind": "Cluster",
            "apiVersion": "kind.x-k8s.io/v1alpha4",
            "nodes": [{"role": "control-plane", "extraMounts": [{"hostPath": str(self.workspaces), "containerPath": NODE_WORKSPACES}]}],
        }
        path = self._dir / "kind.json"
        path.write_text(json.dumps(config), "utf-8")
        args = [self.kind, "create", "cluster", "--name", CLUSTER, "--config", str(path), "--wait", "120s"]
        if os.environ.get("AR_KIND_NODE_IMAGE"):
            args += ["--image", os.environ["AR_KIND_NODE_IMAGE"]]
        self._run(*args, timeout=1200)

    def _kubeconfig(self) -> Path:
        raw = self._run(self.kind, "get", "kubeconfig", "--name", CLUSTER).stdout
        (self._dir / "kubeconfig.yaml").write_text(raw, "utf-8")
        data = self.kubectl_run("config", "view", "--raw", "--minify", "-o", "json").stdout
        path = self._dir / "kubeconfig.json"
        path.write_text(data, "utf-8")
        return path

    def _build_agent(self) -> str:
        build = self._dir / "agent"
        try:
            agentbuild.build(build, linux_static=True, env=self._env)
        except agentbuild.MissingTool as e:
            raise MissingTool(str(e)) from None
        (build / "Dockerfile").write_text("FROM scratch\nCOPY sandbox-agent /sandbox-agent\nUSER 10001\nENTRYPOINT [\"/sandbox-agent\"]\n", "utf-8")
        self._run(self.docker, "build", "-q", "-t", AGENT_IMAGE, str(build), timeout=600)
        return AGENT_IMAGE

    def _python_image(self) -> str:
        """跑命令的镜像。Docker Desktop 拉下来的是多平台镜像，kind load 会找不到别的平台的层；在它上面构建一层，
        得到只有本平台的镜像再装进节点。"""
        if self._run(self.docker, "image", "inspect", PYTHON_IMAGE, check=False).returncode != 0:
            self._run(self.docker, "pull", PYTHON_IMAGE, timeout=900)
        build = self._dir / "python"
        build.mkdir()
        (build / "Dockerfile").write_text(f"FROM {PYTHON_IMAGE}\n", "utf-8")
        self._run(self.docker, "build", "-q", "-t", SANDBOX_PYTHON_IMAGE, str(build), timeout=600)
        return SANDBOX_PYTHON_IMAGE

    def _install_gvisor(self, package: Path) -> None:
        """把 gVisor 装进 kind 的节点并给 containerd 登记 runsc（RuntimeClass gvisor 的 handler）。重复调用是安全的。"""
        sums = package.with_name(package.name + ".sha512")
        if not package.is_file() or not sums.is_file():
            raise MissingTool(f"AR_KIND_GVISOR 要指向 gVisor 的发布包，旁边要有 {sums.name}")
        want = sums.read_text("utf-8").split()[0].lower()
        digest = hashlib.sha512()
        with package.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != want:
            raise RuntimeError(f"{package.name} 的 sha512 对不上")
        node = f"{CLUSTER}-control-plane"
        installed = self._run(self.docker, "exec", node, "sh", "-c", "test -x /usr/local/bin/runsc && test -d /usr/local/bin/gvisor-bin", check=False).returncode == 0
        if not installed:
            out = self._dir / "gvisor"
            out.mkdir()
            self._run(shutil.which("tar") or "tar", "-xf", str(package), "-C", str(out), timeout=600)
            for item in sorted(out.iterdir()):
                self._run(self.docker, "cp", str(item), f"{node}:/usr/local/bin/", timeout=600)
            self._run(self.docker, "exec", node, "sh", "-c",
                      "chmod 0755 /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1 && chmod -R a+rX /usr/local/bin/gvisor-bin "
                      "&& find /usr/local/bin/gvisor-bin -type f -exec chmod a+x {} +")
        config = self._run(self.docker, "exec", node, "cat", "/etc/containerd/config.toml").stdout
        if "runtimes.runsc]" not in config:
            # kind 的节点用 containerd 的 v2 格式配置。
            block = '\n[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]\n  runtime_type = "io.containerd.runsc.v1"\n'
            self._run(self.docker, "exec", "-i", node, "sh", "-c", "cat >> /etc/containerd/config.toml", input=block)
            self._run(self.docker, "exec", node, "systemctl", "restart", "containerd")
            self.kubectl_run("wait", "--for=condition=Ready", "node", "--all", "--timeout=180s")
        self.kubectl_run("apply", "-f", str(REPO_ROOT / "deploy" / "cluster" / "runtimeclasses.yaml"))

    def _wait_bound(self) -> None:
        deadline = time.monotonic() + 60
        while True:
            phase = self.kubectl_run("get", "pvc", "workspaces", "-n", "sandboxes", "-o", "jsonpath={.status.phase}", check=False).stdout.strip()
            if phase == "Bound":
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"sandboxes/workspaces 没有绑定（{phase}）")
            time.sleep(1)

    def workspace_path(self, tenant_id: str, workspace_id: str) -> Path:
        return self.workspaces / tenant_id / workspace_id

    def stop(self) -> None:
        if getattr(self, "created", False) and not self.reuse:
            self._run(self.kind, "delete", "cluster", "--name", CLUSTER, check=False, timeout=300)
        shutil.rmtree(self._dir, ignore_errors=True)
