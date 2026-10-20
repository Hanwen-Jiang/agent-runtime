"""拉起端到端的整套进程：Temporal 开发服务（Temporal CLI）、task-service（Go，现场编译）、Python Worker。

Worker 可以被强杀再重启（计划 S-03 验收“杀 Worker”）。各进程的输出写进临时目录，失败时附在报错里。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
TASK_SERVICE_DIR = REPO_ROOT / "services" / "go-task-service"
WORKER_QUEUES = "task,decision,model,program"

# 端到端测试的 Session 参数：只读 Task 并行上限 2；每接受 3 个 Task 续接一次，让续接在各个用例里都发生。
READ_ONLY_LIMIT = 2
CONTINUE_AS_NEW_TASKS = 3
# 桩 model 节点的 timeout_s；heartbeat 超时由它推出（约 2 秒），Worker 被杀后很快被接手。
STUB_NODE_TIMEOUT_S = 40


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise RuntimeError(f"找不到 {tool}，见 CLAUDE.md 的常用命令")
    return path


def wait_until(check: Any, what: str, timeout: float = 60, interval: float = 0.2) -> Any:
    deadline = time.monotonic() + timeout
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 — 轮询期间的错误只记最后一个
            last = exc
        time.sleep(interval)
    raise TimeoutError(f"{timeout} 秒内没有等到：{what}（最后的错误：{last!r}）")


def tail(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text("utf-8", errors="replace").splitlines()[-lines:])


@dataclass
class Process:
    name: str
    args: list[str]
    env: dict[str, str]
    log: Path
    cwd: Path | None = None
    proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        out = self.log.open("ab")
        self.proc = subprocess.Popen(self.args, env=self.env, cwd=self.cwd, stdout=out, stderr=subprocess.STDOUT)

    def kill(self) -> None:
        """强杀，不给进程收尾的机会。"""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.kill()

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


@dataclass
class Stack:
    root: Path
    temporal_address: str = ""
    http_url: str = ""
    signing_key: str = "e2e-dev-signing-key-0123456789"
    processes: dict[str, Process] = field(default_factory=dict)
    task_service_bin: Path | None = None

    @property
    def call_log(self) -> Path:
        return self.root / "calls.jsonl"

    @property
    def gate_dir(self) -> Path:
        return self.root / "gates"

    @property
    def pins_file(self) -> Path:
        return self.root / "pinned_versions.json"

    def set_pins(self, pins: dict[str, Any]) -> None:
        """改固定版本文件；之后提交的 Task 用新版本（Registry 发布新版本的替身）。"""
        self.pins_file.write_text(json.dumps(pins), "utf-8")

    def open_gate(self, task_id: str) -> None:
        (self.gate_dir / task_id).touch()

    def calls(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if not self.call_log.exists():
            return []
        out = []
        for line in self.call_log.read_text("utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if task_id is None or entry["task_id"] == task_id:
                    out.append(entry)
        return out

    def count(self, task_id: str, activity: str, event: str) -> int:
        return sum(1 for c in self.calls(task_id) if c["activity"] == activity and c["event"] == event)

    def wait_call(self, task_id: str, activity: str, event: str, timeout: float = 60) -> dict[str, Any]:
        def check() -> dict[str, Any] | None:
            return next((c for c in self.calls(task_id) if c["activity"] == activity and c["event"] == event), None)

        return wait_until(check, f"{task_id} 的 {activity} {event}", timeout)

    def token(self, tenant: str, user: str) -> str:
        assert self.task_service_bin is not None
        proc = subprocess.run(
            [str(self.task_service_bin), "dev-token", "--tenant", tenant, "--user", user, "--ttl", "2h"],
            env={**os.environ, "AR_DEV_SIGNING_KEY": self.signing_key},
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=60,
            check=True,
        )
        return proc.stdout.strip()

    # --- 进程 ---

    def start(self) -> None:
        self.gate_dir.mkdir(parents=True, exist_ok=True)
        self.set_pins({"recipe": "stub@v1", "node_templates": {"stub_answer": "v1"}, "model_profiles": {"mock/model": "v1"}, "tenant_policy": "v1"})
        self._start_temporal()
        self._build_task_service()
        self._start_task_service()
        self.start_worker()

    def _start_temporal(self) -> None:
        port, ui_port = free_port(), free_port()
        self.temporal_address = f"127.0.0.1:{port}"
        temporal = require("temporal")
        p = Process(
            "temporal",
            [temporal, "server", "start-dev", "--ip", "127.0.0.1", "--port", str(port), "--ui-port", str(ui_port), "--headless", "--log-level", "error"],
            dict(os.environ),
            self.root / "logs" / "temporal.log",
        )
        p.start()
        self.processes["temporal"] = p
        wait_until(
            lambda: subprocess.run([temporal, "workflow", "list", "--address", self.temporal_address, "--limit", "1"], capture_output=True, timeout=30).returncode == 0,
            "Temporal 开发服务就绪",
        )

    def _build_task_service(self) -> None:
        exe = ".exe" if sys.platform == "win32" else ""
        self.task_service_bin = self.root / f"task-service{exe}"
        proc = subprocess.run(
            [require("go"), "build", "-o", str(self.task_service_bin), "./cmd/task-service"],
            cwd=TASK_SERVICE_DIR,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"编译 task-service 失败：\n{proc.stderr}")

    def _start_task_service(self) -> None:
        port = free_port()
        self.http_url = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "AR_TEMPORAL_ADDRESS": self.temporal_address,
            "AR_HTTP_ADDR": f"127.0.0.1:{port}",
            "AR_PUBLIC_BASE_URL": self.http_url,
            "AR_DEV_SIGNING_KEY": self.signing_key,
            "AR_DATA_DIR": str(self.root / "data"),
            "AR_PINNED_VERSIONS_FILE": str(self.pins_file),
            "AR_SESSION_READ_ONLY_LIMIT": str(READ_ONLY_LIMIT),
            "AR_SESSION_CAN_TASKS": str(CONTINUE_AS_NEW_TASKS),
        }
        assert self.task_service_bin is not None
        p = Process("task-service", [str(self.task_service_bin), "serve"], env, self.root / "logs" / "task-service.log")
        p.start()
        self.processes["task-service"] = p
        wait_until(lambda: urllib.request.urlopen(self.http_url + "/healthz", timeout=2).status == 200, "task-service 就绪")

    def start_worker(self) -> Process:
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "AR_TEMPORAL_ADDRESS": self.temporal_address,
            "AR_STUB_CALL_LOG": str(self.call_log),
            "AR_STUB_GATE_DIR": str(self.gate_dir),
            "AR_STUB_NODE_TIMEOUT_S": str(STUB_NODE_TIMEOUT_S),
            # Node Executor 的开发实现（ObjectStore、SecretStore、Sandbox）的数据目录，与 task-service 的分开。
            "AR_DATA_DIR": str(self.root / "worker-data"),
        }
        p = Process("worker", [sys.executable, "-m", "agentruntime_worker", "--queues", WORKER_QUEUES], env, self.root / "logs" / "worker.log")
        p.start()
        self.processes["worker"] = p
        time.sleep(1)
        if not p.alive():
            raise RuntimeError(f"Worker 启动失败：\n{tail(p.log)}")
        return p

    def kill_worker(self) -> None:
        self.processes["worker"].kill()

    def stop(self) -> None:
        for name in ("worker", "task-service", "temporal"):
            if name in self.processes:
                self.processes[name].stop()

    def diagnostics(self) -> str:
        return "\n".join(f"===== {p.name} =====\n{tail(p.log)}" for p in self.processes.values())
