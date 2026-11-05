"""现场编译并拉起 Model Gateway（services/go-model-gateway 的 model-gateway），供跨语言端到端测试。

照 docs/plan/model-gateway-direct.md 交付 2 的命令与环境变量：
- model-gateway dev-secret <名称>：从标准输入读密钥，写进 AR_SECRETS_FILE（开发 SecretStore）的平台作用域；
- model-gateway serve：AR_MODEL_GATEWAY_ADDR 上同时提供 gRPC（h2c）与 GET /healthz；每次调用往标准输出写一行计量记录（JSON）。
标准输出（计量记录）与标准错误（日志）分开写文件；失败时附在报错里，已知的密钥先替换掉再输出。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from e2e_support.stack import REPO_ROOT, free_port, require, tail, wait_until

GATEWAY_DIR = REPO_ROOT / "services" / "go-model-gateway"
DEV_PROFILES_FILE = REPO_ROOT / "deploy" / "dev" / "model-profiles.json"
# AR_MODEL_PROVIDER_KEY_NAME 的默认值。
DEFAULT_KEY_NAME = "model-provider/api-key"
# 开发 SecretStore 文件里平台作用域的名字（libs/go-core/dev/devutil.PlatformID，与 Python 侧相同）。
PLATFORM_SCOPE = "_platform"
# 子进程不继承的环境变量：真实密钥只经 dev-secret 的标准输入进入 SecretStore，不经环境变量。
_SCRUBBED_ENV = ("AR_MODEL_PROVIDER_API_KEY",)
# 计量记录的字段（约定 4）。
METERING_FIELDS = (
    "tenant_id",
    "task_id",
    "node_id",
    "role",
    "model_profile_id",
    "tier",
    "vendor",
    "input_tokens",
    "output_tokens",
    "cost_usd_micros",
)


def build_gateway(out_dir: Path) -> Path:
    """go build ./cmd/model-gateway，可执行文件放在 out_dir 下。"""
    exe = ".exe" if sys.platform == "win32" else ""
    binary = out_dir / f"model-gateway{exe}"
    proc = subprocess.run(
        [require("go"), "build", "-o", str(binary), "./cmd/model-gateway"],
        cwd=GATEWAY_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"编译 model-gateway 失败：\n{proc.stderr}")
    return binary


def child_env(extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env.update(extra)
    return env


def dev_secret(binary: Path, secrets_file: Path, name: str, value: str) -> subprocess.CompletedProcess[str]:
    """model-gateway dev-secret <名称>：密钥只从标准输入给（末尾带换行，像 echo 那样）。"""
    return subprocess.run(
        [str(binary), "dev-secret", name],
        input=value + "\n",
        env=child_env({"AR_SECRETS_FILE": str(secrets_file)}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def seed_tenant_secret(secrets_file: Path, tenant_id: str, name: str, value: str) -> None:
    """往开发 SecretStore 文件里加租户作用域的密钥（BYOK）。格式见 libs/go-core/dev/filesecrets。网关启动前写。"""
    data: dict[str, Any] = json.loads(secrets_file.read_text("utf-8")) if secrets_file.exists() else {"version": 1, "scopes": {}}
    scope = data.setdefault("scopes", {}).setdefault(tenant_id, {"secrets": {}, "roles": []})
    scope.setdefault("secrets", {})[name] = value
    secrets_file.write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def platform_secret(secrets_file: Path, name: str) -> str | None:
    data = json.loads(secrets_file.read_text("utf-8"))
    return ((data.get("scopes") or {}).get(PLATFORM_SCOPE) or {}).get("secrets", {}).get(name)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


@dataclass
class Gateway:
    """一个 model-gateway serve 进程。"""

    binary: Path
    root: Path
    profiles_file: Path
    secrets_file: Path
    provider: str = "openai_compat"
    base_url: str = ""
    key_name: str = ""
    tenant_policies_file: Path | None = None
    # 失败输出里要替换掉的密钥。
    secrets: list[str] = field(default_factory=list)
    address: str = ""
    proc: subprocess.Popen[bytes] | None = None

    @property
    def stdout_path(self) -> Path:
        return self.root / "model-gateway.stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.root / "model-gateway.stderr.log"

    def env(self) -> dict[str, str]:
        extra = {
            "AR_MODEL_GATEWAY_ADDR": self.address,
            "AR_MODEL_PROFILES_FILE": str(self.profiles_file),
            "AR_MODEL_PROVIDER": self.provider,
            "AR_SECRETS_FILE": str(self.secrets_file),
            # 预算账本（R-06 的 model_usage）的 SQLite：不设时网关写进当前目录下的 data/（跑测试时是 tests/e2e/data）。
            "AR_DATA_DIR": str(self.root / "data"),
        }
        if self.base_url:
            extra["AR_MODEL_PROVIDER_BASE_URL"] = self.base_url
        if self.key_name:
            extra["AR_MODEL_PROVIDER_KEY_NAME"] = self.key_name
        if self.tenant_policies_file is not None:
            extra["AR_DEV_TENANT_POLICIES_FILE"] = str(self.tenant_policies_file)
        return child_env(extra)

    def start(self, timeout: float = 60) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.address = f"127.0.0.1:{free_port()}"
        out = self.stdout_path.open("ab")
        err = self.stderr_path.open("ab")
        self.proc = subprocess.Popen([str(self.binary), "serve"], env=self.env(), stdout=out, stderr=err)
        try:
            wait_until(self._healthy, "model-gateway 就绪（GET /healthz 返回 200）", timeout)
        except TimeoutError as exc:
            raise RuntimeError(f"model-gateway 没有起来：{exc}\n{self.diagnostics()}") from None

    def _healthy(self) -> bool:
        if self.proc is not None and self.proc.poll() is not None:
            raise RuntimeError(f"model-gateway 已退出（退出码 {self.proc.returncode}）")
        with urllib.request.urlopen(f"http://{self.address}/healthz", timeout=2) as resp:
            return resp.status == 200

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)

    def stdout(self) -> str:
        return self.stdout_path.read_text("utf-8", errors="replace") if self.stdout_path.exists() else ""

    def stderr(self) -> str:
        return self.stderr_path.read_text("utf-8", errors="replace") if self.stderr_path.exists() else ""

    def output(self) -> str:
        """标准输出与标准错误的全部内容（断言密钥不外泄用）。"""
        return self.stdout() + "\n" + self.stderr()

    def metering(self) -> list[dict[str, Any]]:
        """标准输出里的计量记录：每行一个 JSON 对象，带 model_profile_id。"""
        records = []
        for line in self.stdout().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and "model_profile_id" in entry:
                records.append(entry)
        return records

    def metering_for(self, *, tenant_id: str, task_id: str, node_id: str) -> list[dict[str, Any]]:
        return [r for r in self.metering() if r.get("tenant_id") == tenant_id and r.get("task_id") == task_id and r.get("node_id") == node_id]

    def diagnostics(self) -> str:
        text = f"===== model-gateway stdout =====\n{tail(self.stdout_path)}\n===== model-gateway stderr =====\n{tail(self.stderr_path)}"
        return redact(text, self.secrets)
