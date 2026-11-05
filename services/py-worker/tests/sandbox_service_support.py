"""测试用的 Sandbox Service（SBX-01）：编译 services/go-sandbox-service 与 sandbox-agent（默认 Rust 版，见 py-core 的 agentbuild，
D-28），起一个真的服务进程。

Worker 的沙箱一律经它（不再有进程内的 Broker 与后端适配器）。服务的业务库、对象存储、密钥文件与工作区根都在测试的临时目录里；
本地后端（kind: local）每个环境起一个 sandbox-agent 进程，环境不随服务退出（服务被杀后按环境表接上）。
缺 go（或编译 agent 的 cargo）时测试失败，设 AR_ALLOW_MISSING_TOOLS=1 改为跳过。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
WORKER_TOKEN = "worker-token-0123456789abcdef0123456789abcdef"
READER_TOKEN = "task-service-token-0123456789abcdef0123456789"
_BUILT: dict[str, Path] = {}
# 这个用例起的服务：conftest 的 autouse 夹具在用例结束时全部停掉（Setup 不是夹具，自己收不了尾）。
RUNNING: list[SandboxService] = []


def _go() -> str:
    go = shutil.which("go")
    if go is None:
        if os.environ.get("AR_ALLOW_MISSING_TOOLS") == "1":
            pytest.skip("PATH 上没有 go（Sandbox Service 的测试要编译它）")
        pytest.fail("PATH 上没有 go：Sandbox Service 的测试要编译 services/go-sandbox-service", pytrace=False)
    return go


def binaries(out: Path) -> tuple[Path, Path]:
    """编译服务与 sandbox-agent（同一个 pytest 进程只编一次）。"""
    if "service" not in _BUILT:
        from agentruntime_core.adapters import agentbuild

        go = _go()
        out.mkdir(parents=True, exist_ok=True)
        target = out / "sandbox-service"
        subprocess.run([go, "build", "-o", str(target), "./services/go-sandbox-service/cmd/sandbox-service"], cwd=REPO, check=True)
        _BUILT["service"] = target
        try:
            _BUILT["agent"] = agentbuild.build(out / "agent")
        except agentbuild.MissingTool as err:
            if os.environ.get("AR_ALLOW_MISSING_TOOLS") == "1":
                pytest.skip(str(err))
            pytest.fail(str(err), pytrace=False)
    return _BUILT["service"], _BUILT["agent"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class SandboxService:
    """一个服务进程。workspaces、data 等目录在 root 下；restart、kill 模拟服务被杀。"""

    def __init__(self, root: Path, backends: Mapping[str, Any] | None = None, *, policies: Mapping[str, Any] | None = None,
                 env: Mapping[str, str] | None = None, bin_dir: Path | None = None) -> None:
        self.root = root
        self.workspaces = root / "workspaces"
        self.data = root / "data"
        self.objects = root / "objects"
        self.state = root / "sandboxes"
        self.secrets_file = root / "secrets.json"
        self.backends_file = root / "backends.json"
        self.policies_file = root / "policies.json"
        self.port = free_port()
        self.address = f"127.0.0.1:{self.port}"
        self.extra_env = dict(env or {})
        for d in (self.workspaces, self.data, self.objects, self.state):
            d.mkdir(parents=True, exist_ok=True)
        self.backends_file.write_text(json.dumps(backends or {"default": "local", "backends": {"local": {"kind": "local", "isolation": "none"}}}), "utf-8")
        self.policies_file.write_text(json.dumps({"tenants": dict(policies or {})}), "utf-8")
        self.secrets_file.write_text(json.dumps({"version": 1, "scopes": {"_platform": {"secrets": {
            "sandbox-service/tokens/worker": WORKER_TOKEN,
            "sandbox-service/tokens/task-service": READER_TOKEN,
        }}}}), "utf-8")
        self.service_bin, self.agent_bin = binaries(bin_dir or Path(os.environ.get("TMPDIR", "/tmp")) / f"ar-sbx-bin-{os.getpid()}")
        self.process: subprocess.Popen[bytes] | None = None
        self.log = root / "sandbox-service.log"

    def environ(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", "/tmp"),
            "AR_SANDBOX_SERVICE_ADDR": self.address,
            "AR_SANDBOX_BACKENDS_FILE": str(self.backends_file),
            "AR_TOOL_WORKSPACE_ROOT": str(self.workspaces),
            "AR_DATA_DIR": str(self.data),
            "AR_OBJECT_STORE_DIR": str(self.objects),
            "AR_SECRETS_FILE": str(self.secrets_file),
            "AR_DEV_TENANT_POLICIES_FILE": str(self.policies_file),
            "AR_SANDBOX_STATE_DIR": str(self.state),
            "AR_SANDBOX_AGENT_BIN": str(self.agent_bin),
            "AR_SANDBOX_RECONCILE_INTERVAL_S": "3600",
            "AR_SANDBOX_POLICY_TTL_S": "0",
            **self.extra_env,
        }

    def start(self) -> None:
        if self not in RUNNING:
            RUNNING.append(self)
        with self.log.open("ab") as log:
            self.process = subprocess.Popen([str(self.service_bin), "serve"], env=self.environ(), stdout=log, stderr=log)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Sandbox Service 没起来：" + self.log.read_text("utf-8", errors="replace")[-3000:])
            try:
                with urllib.request.urlopen(f"http://{self.address}/healthz", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("Sandbox Service 30 秒内没有就绪：" + self.log.read_text("utf-8", errors="replace")[-3000:])

    def kill(self) -> None:
        """模拟服务被杀（SIGKILL，不收尾）。"""
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait(timeout=10)
        self.process = None

    def restart(self) -> None:
        self.kill()
        self.start()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        # 本地后端的 agent 进程不随服务退出：测试结束时收掉。
        subprocess.run([str(self.service_bin), "kill-local", str(self.state)], check=False, env=self.environ())

    def write_policies(self, policies: Mapping[str, Any]) -> None:
        """改租户策略（开发用的策略文件；服务按 AR_POLICY_TTL 缓存，测试里设成 0）。"""
        self.policies_file.write_text(json.dumps({"tenants": dict(policies)}), "utf-8")

    def worker_env(self) -> dict[str, str]:
        """Worker 连这个服务要的环境变量。"""
        return {"AR_SANDBOX_SERVICE_ADDRESS": self.address, "AR_SECRETS_FILE": str(self.secrets_file)}
