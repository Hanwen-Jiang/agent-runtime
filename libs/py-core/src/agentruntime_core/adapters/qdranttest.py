"""测试里拉起临时的 Qdrant 实例（计划 S-12b）。

二进制取自 AR_QDRANT_BIN 指定的文件或目录，或 PATH 上的 qdrant（本机装在 %USERPROFILE%\\.local\\qdrant，见 CLAUDE.md），
不让测试自行下载。找不到时测试失败；设 AR_ALLOW_MISSING_TOOLS=1 改为跳过。

存储放在临时目录（本地盘，路径尽量短：Windows 上 Qdrant 的段文件路径受 260 字符限制），HTTP 与 gRPC 端口随机，关掉遥测。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

ENV_BIN = "AR_QDRANT_BIN"


class MissingTool(RuntimeError):
    """找不到 Qdrant 的二进制。"""


def _binary() -> str:
    exe = "qdrant" + (".exe" if os.name == "nt" else "")
    configured = os.environ.get(ENV_BIN, "")
    if configured:
        path = Path(configured)
        if path.is_dir():
            path = path / exe
        if not path.exists():
            raise MissingTool(f"{ENV_BIN} 指向的位置没有 {exe}")
        return str(path)
    found = shutil.which("qdrant")
    if found is None:
        raise MissingTool("找不到 qdrant（Qdrant 的二进制），见 CLAUDE.md 的常用命令")
    return found


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Instance:
    """一个临时的 Qdrant 进程。url 是 HTTP 地址。"""

    def __init__(self, *, ready_timeout: float = 60) -> None:
        binary = _binary()
        self.dir = Path(tempfile.mkdtemp(prefix="arq-"))
        self.http_port = _free_port()
        self.grpc_port = _free_port()
        env = {
            **os.environ,
            "QDRANT__SERVICE__HOST": "127.0.0.1",
            "QDRANT__SERVICE__HTTP_PORT": str(self.http_port),
            "QDRANT__SERVICE__GRPC_PORT": str(self.grpc_port),
            "QDRANT__STORAGE__STORAGE_PATH": str(self.dir / "s"),
            "QDRANT__STORAGE__SNAPSHOTS_PATH": str(self.dir / "snap"),
            "QDRANT__TELEMETRY_DISABLED": "true",
        }
        self._log = open(self.dir / "qdrant.log", "w", encoding="utf-8")  # noqa: SIM115 — 进程结束时关闭
        self.process = subprocess.Popen([binary], cwd=self.dir, env=env, stdout=self._log, stderr=subprocess.STDOUT)
        self.url = f"http://127.0.0.1:{self.http_port}"
        deadline = time.monotonic() + ready_timeout
        while True:
            if self.process.poll() is not None:
                raise RuntimeError(f"qdrant 启动失败：{(self.dir / 'qdrant.log').read_text('utf-8', errors='replace')}")
            try:
                with urllib.request.urlopen(self.url + "/readyz", timeout=2) as resp:
                    if resp.status == 200:
                        return
            except OSError:
                pass
            if time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("qdrant 没有在时限内就绪")
            time.sleep(0.2)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._log.close()
        shutil.rmtree(self.dir, ignore_errors=True)
