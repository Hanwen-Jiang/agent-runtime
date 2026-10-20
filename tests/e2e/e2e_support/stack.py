"""拉起端到端的整套进程：Temporal 开发服务（Temporal CLI）、task-service（Go，现场编译）、Python Worker。

Worker 可以被强杀再重启（计划 S-03 验收“杀 Worker”）。故障注入套件（计划 S-07）另外用到：
- Worker 切换成故障 Worker（e2e_support.fault_worker），按故障计划出错；
- task-service 在同一端口上强杀再重启：开发环境的 Cache 在 task-service 进程内，这就是“Cache 重启”；
- 租户策略文件：写操作租户（WRITE_TENANT）的 effect 上限是 write_irreversible，其余租户用默认的 read。
各进程的输出写进临时目录，失败时附在报错里。
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

from e2e_support.faults import ENV_FAULT_LEDGER_DIR, ENV_FAULT_PLAN, FaultPlan
from e2e_support.ledger import ExternalLedger

REPO_ROOT = Path(__file__).resolve().parents[3]
TASK_SERVICE_DIR = REPO_ROOT / "services" / "go-task-service"
WORKER_QUEUES = "task,decision,model,program"

# 端到端测试的 Session 参数：只读 Task 并行上限 2；每接受 3 个 Task 续接一次，让续接在各个用例里都发生。
READ_ONLY_LIMIT = 2
CONTINUE_AS_NEW_TASKS = 3
# 桩 model 节点的 timeout_s；heartbeat 超时由它推出（约 2 秒），Worker 被杀后很快被接手。
STUB_NODE_TIMEOUT_S = 40
# task-service 的开发租户策略文件（租户 → effect 上限等）；没列出的租户用默认值。
ENV_TENANT_POLICIES = "AR_DEV_TENANT_POLICIES_FILE"
# 允许写操作的租户，故障注入套件的写操作用例用它。
WRITE_TENANT = "tw"
# ObjectStore 与 SecretStore 开发实现的位置（task-service 与 Worker 相同的变量名，S-06）。
ENV_OBJECT_STORE_DIR = "AR_OBJECT_STORE_DIR"
ENV_SECRETS_FILE = "AR_SECRETS_FILE"


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
    # standard：python -m agentruntime_worker；fault：python -m e2e_support.fault_worker。
    worker_mode: str = "standard"
    # 重启 task-service 时附加的环境变量（例如更低的限流上限）。
    task_service_env: dict[str, str] = field(default_factory=dict)
    http_port: int = 0

    @property
    def call_log(self) -> Path:
        return self.root / "calls.jsonl"

    @property
    def faults(self) -> FaultPlan:
        return FaultPlan(self.root / "faults.json")

    @property
    def ledger(self) -> ExternalLedger:
        return ExternalLedger(self.root / "external")

    @property
    def tenant_policies_file(self) -> Path:
        return self.root / "tenant_policies.json"

    @property
    def objects_dir(self) -> Path:
        """task-service 与 Worker 共用的 ObjectStore（开发实现）目录：Claim-Check 的对象两边都要读得到（S-06）。"""
        return self.root / "shared" / "objects"

    @property
    def secrets_file(self) -> Path:
        """task-service 与 Worker 共用的 SecretStore（开发实现）文件：两边用同一把租户密钥加密密钥。"""
        return self.root / "shared" / "secrets.json"

    def shared_store_env(self) -> dict[str, str]:
        """ObjectStore 与 SecretStore 的环境变量；回放工具解码带引用的事件历史也要它们。"""
        return {ENV_OBJECT_STORE_DIR: str(self.objects_dir), ENV_SECRETS_FILE: str(self.secrets_file)}

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
        self.tenant_policies_file.write_text(json.dumps({"tenants": {WRITE_TENANT: {"effect_ceiling": "write_irreversible"}}}), "utf-8")
        self.faults.clear()
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
        # 重启时沿用同一端口：客户端手里的地址（SSE 订阅地址等）不变。
        self.http_port = self.http_port or free_port()
        self.http_url = f"http://127.0.0.1:{self.http_port}"
        env = {
            **os.environ,
            "AR_TEMPORAL_ADDRESS": self.temporal_address,
            "AR_HTTP_ADDR": f"127.0.0.1:{self.http_port}",
            "AR_PUBLIC_BASE_URL": self.http_url,
            "AR_DEV_SIGNING_KEY": self.signing_key,
            "AR_DATA_DIR": str(self.root / "data"),
            "AR_PINNED_VERSIONS_FILE": str(self.pins_file),
            "AR_SESSION_READ_ONLY_LIMIT": str(READ_ONLY_LIMIT),
            "AR_SESSION_CAN_TASKS": str(CONTINUE_AS_NEW_TASKS),
            ENV_TENANT_POLICIES: str(self.tenant_policies_file),
            **self.shared_store_env(),
            **self.task_service_env,
        }
        assert self.task_service_bin is not None
        for attempt in range(3):
            p = Process("task-service", [str(self.task_service_bin), "serve"], env, self.root / "logs" / "task-service.log")
            p.start()
            self.processes["task-service"] = p
            time.sleep(0.5)
            if p.alive() or attempt == 2:
                break
            time.sleep(1)  # 端口还没释放：稍等再起
        wait_until(lambda: urllib.request.urlopen(self.http_url + "/healthz", timeout=2).status == 200, "task-service 就绪")

    def restart_task_service(self, env: dict[str, str] | None = None) -> None:
        """强杀 task-service 再在同一端口上拉起。开发环境的 Cache 在进程内，随之清空（计划 S-07 场景 9）；
        业务库（SQLite）与 Temporal 里的状态不受影响。env 替换附加的环境变量，None 表示恢复默认。"""
        self.processes["task-service"].kill()
        self.task_service_env = dict(env or {})
        self._start_task_service()

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
            **self.shared_store_env(),
            ENV_FAULT_PLAN: str(self.faults.path),
            ENV_FAULT_LEDGER_DIR: str(self.ledger.root),
        }
        module = "e2e_support.fault_worker" if self.worker_mode == "fault" else "agentruntime_worker"
        p = Process("worker", [sys.executable, "-m", module, "--queues", WORKER_QUEUES], env, self.root / "logs" / "worker.log")
        p.start()
        self.processes["worker"] = p
        time.sleep(1)
        if not p.alive():
            raise RuntimeError(f"Worker 启动失败：\n{tail(p.log)}")
        return p

    def kill_worker(self) -> None:
        self.processes["worker"].kill()

    def restart_worker(self) -> Process:
        self.kill_worker()
        return self.start_worker()

    def use_worker(self, mode: str) -> None:
        """换成另一种 Worker（standard 或 fault）。"""
        if mode not in ("standard", "fault"):
            raise ValueError(mode)
        if mode == self.worker_mode and self.worker_alive():
            return
        if "worker" in self.processes:
            self.kill_worker()
        self.worker_mode = mode
        self.start_worker()

    def worker_alive(self) -> bool:
        return "worker" in self.processes and self.processes["worker"].alive()

    def wait_worker_exit(self, timeout: float = 60) -> int:
        """等 Worker 自己退出（故障计划里的 crash），返回退出码。"""
        p = self.processes["worker"]
        wait_until(lambda: not p.alive(), "Worker 按故障计划退出", timeout)
        assert p.proc is not None
        return p.proc.returncode

    def ensure_healthy(self) -> None:
        """用例结束后恢复：Worker 活着、task-service 用默认配置运行、没有故障规则。"""
        self.faults.clear()
        if self.task_service_env or not self.processes["task-service"].alive():
            self.restart_task_service(None)
        if not self.worker_alive():
            self.start_worker()

    def stop(self) -> None:
        for name in ("worker", "task-service", "temporal"):
            if name in self.processes:
                self.processes[name].stop()

    def diagnostics(self) -> str:
        return "\n".join(f"===== {p.name} =====\n{tail(p.log)}" for p in self.processes.values())
