"""端到端测试用的一次性 Valkey（TSP-2 的两副本用例）：与 Go 侧 libs/go-core/adapters/valkey/valkeytest 同样的做法。

用 PATH 上（或 AR_DOCKER_BIN 指定位置）的 docker 起容器：不落盘、端口只绑 127.0.0.1、随机口令，用完删除。
镜像默认与 valkeytest、deploy/data/valkey.yaml 相同，可用 AR_VALKEY_IMAGE 覆盖。设了 AR_VALKEY_TEST_URL 时直接用它。
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

DEFAULT_IMAGE = "valkey/valkey:9.1.2"


def _docker() -> str:
    configured = os.environ.get("AR_DOCKER_BIN")
    if configured:
        return configured
    found = shutil.which("docker")
    if found is None:
        raise RuntimeError("找不到 docker（两副本用例用它拉起 Valkey），见 CLAUDE.md 的常用命令")
    return found


@dataclass
class Valkey:
    url: str
    container: str = ""
    docker: str = ""

    @classmethod
    def start(cls) -> Valkey:
        existing = os.environ.get("AR_VALKEY_TEST_URL")
        if existing:
            return cls(url=existing)
        docker = _docker()
        password = secrets.token_hex(16)
        name = f"ar-valkey-e2e-{secrets.token_hex(4)}"
        image = os.environ.get("AR_VALKEY_IMAGE", DEFAULT_IMAGE)
        run = subprocess.run(
            [docker, "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::6379", image,
             "valkey-server", "--save", "", "--appendonly", "no", "--requirepass", password],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        if run.returncode != 0:
            raise RuntimeError(f"docker run {image} 失败：\n{run.stderr}")
        v = cls(url="", container=name, docker=docker)
        try:
            port = subprocess.run([docker, "port", name, "6379/tcp"], capture_output=True, text=True, timeout=60, check=True).stdout
            addr = next(line.strip() for line in port.splitlines() if line.strip().startswith("127.0.0.1:"))
            v.url = f"valkey://:{password}@{addr}/0"
            v._wait_ready(addr, password)
        except Exception:
            v.stop()
            raise
        return v

    @staticmethod
    def _wait_ready(addr: str, password: str, timeout: float = 60) -> None:
        host, _, port = addr.rpartition(":")
        auth = f"*2\r\n$4\r\nAUTH\r\n${len(password)}\r\n{password}\r\n*1\r\n$4\r\nPING\r\n".encode()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, int(port)), timeout=2) as s:
                    s.sendall(auth)
                    got = b""
                    while b"PONG" not in got and not got.startswith(b"-"):
                        chunk = s.recv(64)
                        if not chunk:
                            break
                        got += chunk
                    if b"PONG" in got:
                        return
            except OSError:
                pass
            time.sleep(0.2)
        raise TimeoutError(f"{timeout} 秒内 Valkey 没有就绪")

    def stop(self) -> None:
        if self.container:
            subprocess.run([self.docker, "rm", "-f", self.container], capture_output=True, timeout=60)
            self.container = ""
