"""测试里拉起临时的 Milvus 实例（设计改动草稿 C1；T-18）。

用 docker 起一个单机版容器：内嵌 etcd、本地存储（官方 standalone_embed.sh 的做法），不需要单独的 etcd 与 MinIO。
docker 取自 AR_DOCKER_BIN 指定的文件或目录，或 PATH 上的 docker（本机是 Docker Desktop，WSL 2 后端），不让测试自行安装。
镜像默认 milvusdb/milvus:v3.0.2，与客户端 pymilvus 3.0.2 同版；可用 AR_MILVUS_IMAGE 覆盖。
找不到 docker 或 Docker 没在运行时测试失败；设 AR_ALLOW_MISSING_TOOLS=1 改为跳过。

- 数据只在容器里，不挂载宿主目录：仓库在网络盘上，临时目录又可能在 Docker 看不到的位置。内嵌 etcd 的配置在容器里生成。
- gRPC（19530）与健康检查（9091）映射到 127.0.0.1 的随机端口，不对外暴露；结束时删除容器。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

ENV_BIN = "AR_DOCKER_BIN"
ENV_IMAGE = "AR_MILVUS_IMAGE"
DEFAULT_IMAGE = "milvusdb/milvus:v3.0.2"

_EMBED_ETCD = (
    "listen-client-urls: http://0.0.0.0:2379\n"
    "advertise-client-urls: http://0.0.0.0:2379\n"
    "quota-backend-bytes: 4294967296\n"
    "auto-compaction-mode: revision\n"
    "auto-compaction-retention: '1000'\n"
)
_START = f"cat > /milvus/configs/embedEtcd.yaml <<'EOF'\n{_EMBED_ETCD}EOF\nexec milvus run standalone"


class MissingTool(RuntimeError):
    """找不到 docker，或 Docker 没在运行。"""


def _binary() -> str:
    exe = "docker" + (".exe" if os.name == "nt" else "")
    configured = os.environ.get(ENV_BIN, "")
    if configured:
        path = Path(configured)
        if path.is_dir():
            path = path / exe
        if not path.exists():
            raise MissingTool(f"{ENV_BIN} 指向的位置没有 {exe}")
        return str(path)
    found = shutil.which("docker")
    if found is None:
        raise MissingTool("找不到 docker（Milvus 的测试用 Docker 拉起单机版容器），见 CLAUDE.md 的常用命令")
    return found


class Instance:
    """一个临时的 Milvus 单机版容器。uri 是 gRPC 地址（pymilvus 的 uri 参数）。"""

    def __init__(self, *, ready_timeout: float = 300) -> None:
        self.docker = _binary()
        # docker 的凭据助手（docker-credential-desktop）与 docker 在同一目录，子进程的 PATH 要带上它。
        self._env = {**os.environ, "PATH": str(Path(self.docker).parent) + os.pathsep + os.environ.get("PATH", "")}
        self.image = os.environ.get(ENV_IMAGE) or DEFAULT_IMAGE
        if self._docker("info", "--format", "{{.ServerVersion}}", check=False).returncode != 0:
            raise MissingTool("Docker 没在运行（docker info 失败），先启动 Docker Desktop")
        if self._docker("image", "inspect", self.image, check=False).returncode != 0:
            self._docker("pull", self.image, timeout=1800)
        self.name = f"ar-milvus-{uuid.uuid4().hex[:8]}"
        self._docker(
            "run", "-d", "--rm", "--name", self.name,
            "--security-opt", "seccomp=unconfined",
            # 3.0 起不设 DEPLOY_MODE 就按分布式部署处理，内嵌 etcd 会直接 panic。
            "-e", "DEPLOY_MODE=STANDALONE",
            "-e", "ETCD_USE_EMBED=true",
            "-e", "ETCD_DATA_DIR=/var/lib/milvus/etcd",
            "-e", "ETCD_CONFIG_PATH=/milvus/configs/embedEtcd.yaml",
            "-e", "COMMON_STORAGETYPE=local",
            "-p", "127.0.0.1::19530", "-p", "127.0.0.1::9091",
            "--entrypoint", "/bin/sh", self.image, "-c", _START,
        )
        try:
            self.grpc_port = self._port(19530)
            self.health_port = self._port(9091)
            self.uri = f"http://127.0.0.1:{self.grpc_port}"
            self._wait_ready(ready_timeout)
        except BaseException:
            self.stop()
            raise

    def _docker(self, *args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([self.docker, *args], env=self._env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if check and result.returncode != 0:
            raise RuntimeError(f"docker {args[0]} 失败：{result.stderr.strip() or result.stdout.strip()}")
        return result

    def _port(self, container_port: int) -> int:
        out = self._docker("port", self.name, f"{container_port}/tcp").stdout.strip().splitlines()
        return int(out[0].rsplit(":", 1)[1])

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{self.health_port}/healthz"
        while True:
            if self._docker("inspect", "--format", "{{.State.Running}}", self.name, check=False).stdout.strip() != "true":
                raise RuntimeError(f"milvus 容器退出了：{self.logs()}")
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"milvus 没有在时限内就绪：{self.logs()}")
            time.sleep(1)

    def logs(self, tail: int = 40) -> str:
        result = self._docker("logs", "--tail", str(tail), self.name, check=False)
        return (result.stdout + result.stderr).strip()

    def stop(self) -> None:
        self._docker("rm", "-f", self.name, check=False)
