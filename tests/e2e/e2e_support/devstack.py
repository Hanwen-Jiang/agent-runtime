"""`scripts/dev_stack.py`（T-13 交付 1）的测试端：以子进程调用脚本，读 `--json` 的输出。

live 用例与人手动演示用同一个脚本拉起整套，这里不另写一套进程管理。脚本的输出约定（docs/plan/live-e2e.md 第 2 节第 1 条）：
`up --json`、`status --json` 的最后一行是一个 JSON 对象：

    {"root": ..., "task_api": "http://127.0.0.1:<port>", "temporal": "127.0.0.1:<port>", "registry": "127.0.0.1:<port>",
     "model_gateway": "127.0.0.1:<port>", "workspace_root": ..., "shared_env": {"AR_OBJECT_STORE_DIR": ..., "AR_SECRETS_FILE": ...},
     "components": {"<名字>": {"pid": ..., "alive": ..., "log": ..., "stdout": ...（只有 model-gateway）}}}
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from e2e_support.stack import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "dev_stack.py"
COMPONENTS = ("temporal", "task-service", "registry", "model-gateway", "worker")
# T-13 live 用例的租户与用户：effect 上限 write_reversible（docs/plan/live-e2e.md 第 3 节第 3 条）。
LIVE_TENANT = "t13"
LIVE_USER = "u13"


def run_script(*args: str, timeout: float = 900, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})},
        cwd=REPO_ROOT,
    )


def last_json(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"dev_stack.py 没有输出（退出码 {proc.returncode}）：\n{proc.stderr}")
    return json.loads(lines[-1])


@dataclass
class DevStack:
    root: Path
    info: dict[str, Any] = field(default_factory=dict, repr=False)
    # 要从失败输出与检查里认出来的密钥（只在 live 用例里有）。不进 repr：pytest 的失败输出会打印 fixture 的值。
    secrets: list[str] = field(default_factory=list, repr=False)

    @classmethod
    def up(cls, root: Path, *args: str, secrets: list[str] | None = None) -> DevStack:
        proc = run_script("up", "--root", str(root), "--json", *args)
        stack = cls(root=root, secrets=list(secrets or []))
        if proc.returncode != 0:
            raise RuntimeError(stack.redact(f"dev_stack.py up 失败（退出码 {proc.returncode}）：\n{proc.stdout}\n{proc.stderr}"))
        stack.info = last_json(proc)
        return stack

    def down(self) -> subprocess.CompletedProcess[str]:
        return run_script("down", "--root", str(self.root))

    def status(self) -> dict[str, Any]:
        proc = run_script("status", "--root", str(self.root), "--json")
        assert proc.returncode == 0, proc.stderr
        return last_json(proc)

    def token(self, tenant: str, user: str, *scopes: str) -> str:
        args = ["token", "--root", str(self.root), "--tenant", tenant, "--user", user]
        for scope in scopes:
            args += ["--scope", scope]
        proc = run_script(*args, timeout=120)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip().splitlines()[-1]

    def kill(self, component: str) -> None:
        """强杀一个组件（不给它收尾的机会）。"""
        proc = run_script("kill", component, "--root", str(self.root))
        assert proc.returncode == 0, proc.stderr

    def start(self, component: str) -> None:
        proc = run_script("start", component, "--root", str(self.root), timeout=300)
        assert proc.returncode == 0, self.redact(proc.stderr)
        self.info = self.status()

    # --- 地址与文件 ---

    @property
    def task_api(self) -> str:
        return str(self.info["task_api"])

    @property
    def temporal(self) -> str:
        return str(self.info["temporal"])

    @property
    def shared_env(self) -> dict[str, str]:
        return dict(self.info["shared_env"])

    @property
    def workspace_root(self) -> Path:
        return Path(self.info["workspace_root"])

    def log(self, component: str) -> Path:
        return Path(self.info["components"][component]["log"])

    def all_output(self) -> str:
        """各组件的日志（含网关的标准输出），检查密钥不外泄用。"""
        parts = []
        for entry in self.info["components"].values():
            for key in ("log", "stdout"):
                path = entry.get(key)
                if path and Path(path).exists():
                    parts.append(Path(path).read_text("utf-8", errors="replace"))
        return "\n".join(parts)

    def metering(self) -> list[dict[str, Any]]:
        """网关标准输出里的计量记录（每行一个 JSON，带 model_profile_id）。"""
        path = Path(self.info["components"]["model-gateway"]["stdout"])
        records = []
        for line in path.read_text("utf-8", errors="replace").splitlines() if path.exists() else []:
            line = line.strip()
            if line.startswith("{"):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and "model_profile_id" in entry:
                    records.append(entry)
        return records

    def seed_workspace(self, tenant: str, workspace: str, source: Path) -> Path:
        """把样本仓库复制成 <工作区根>/<租户>/<workspace>（先清掉上一次的）。"""
        target = self.workspace_root / tenant / workspace
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        return target

    def redact(self, text: str) -> str:
        for s in self.secrets:
            if s:
                text = text.replace(s, "***")
        return text

    def diagnostics(self) -> str:
        out = []
        for name, entry in (self.info.get("components") or {}).items():
            path = entry.get("log")
            if path and Path(path).exists():
                lines = Path(path).read_text("utf-8", errors="replace").splitlines()[-40:]
                out.append(f"===== {name} =====\n" + "\n".join(lines))
        return self.redact("\n".join(out))
