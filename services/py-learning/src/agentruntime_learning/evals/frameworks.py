"""DeepEval 与 Ragas 的接法（docs/plan/feedback-2026-11-01.md N-12、N-13）：两个框架不进本仓库的依赖，装在单独的 Python 里
（services/py-learning/eval-requirements.txt），由这里起它们的桥（evals/bridges/*.py，只用标准库与框架本身），JSONL 进出。
同 SWE-bench 的官方判分工具的做法。

判分的模型经 OpenAI 兼容的接口。地址、密钥按顺序取：AR_JUDGE_BASE_URL 与 AR_JUDGE_API_KEY；没有时取 AR_MODEL_PROVIDER_BASE_URL 与
AR_MODEL_PROVIDER_API_KEY（同评测的离线对照，provider_gateway）；再没有时读 ~/.agentruntime/model-provider.env。模型由调用方给（--judge-model），
要与被测对象不是同一家（rubric 的规矩，S-08）。

给桥的环境只有 PATH、HOME、语言设置与判分模型的几个变量：本进程别的环境变量（令牌、别的密钥）不带过去。桥失败时的错误说明把密钥换成掩码。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BRIDGES = Path(__file__).resolve().parent / "bridges"
REQUIREMENTS = Path(__file__).resolve().parents[3] / "eval-requirements.txt"
PROVIDER_FILE = Path.home() / ".agentruntime" / "model-provider.env"
# Windows 的 asyncio/_overlapped 初始化 Winsock 需要 SystemRoot；不传会在导入 DeepEval 时失败。
PASS_THROUGH = ("PATH", "HOME", "SYSTEMROOT", "SystemRoot", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
DEFAULT_TIMEOUT_S = 6 * 3600


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class JudgeConfig:
    base_url: str
    api_key: str
    model: str
    embedding_model: str = ""

    def __repr__(self) -> str:  # 不把密钥打进日志与报错
        return f"JudgeConfig(base_url={self.base_url!r}, model={self.model!r}, embedding_model={self.embedding_model!r})"

    def env(self) -> dict[str, str]:
        out = {"AR_JUDGE_BASE_URL": self.base_url, "AR_JUDGE_API_KEY": self.api_key, "AR_JUDGE_MODEL": self.model}
        if self.embedding_model:
            out["AR_JUDGE_EMBEDDING_MODEL"] = self.embedding_model
        return out


def _env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip().removeprefix("export ").strip()] = value.strip().strip('"').strip("'")
    return out


def judge_config(model: str, *, embedding_model: str = "", env: Mapping[str, str] | None = None, provider_file: Path | None = None) -> JudgeConfig:
    """判分模型的配置；地址或密钥都取不到时报 ValueError（不说密钥的值）。"""
    if not model:
        raise ValueError("要给判分的模型（--judge-model），且与被测对象不是同一家")
    env = dict(os.environ if env is None else env)
    base, key = env.get("AR_JUDGE_BASE_URL", ""), env.get("AR_JUDGE_API_KEY", "")
    if not (base and key):
        base, key = env.get("AR_MODEL_PROVIDER_BASE_URL", ""), env.get("AR_MODEL_PROVIDER_API_KEY", "")
    if not (base and key):
        file = _env_file(provider_file or PROVIDER_FILE)
        base, key = file.get("AR_MODEL_PROVIDER_BASE_URL", ""), file.get("AR_MODEL_PROVIDER_API_KEY", "")
    if not (base and key):
        raise ValueError("取不到判分模型的地址与密钥：设 AR_JUDGE_BASE_URL、AR_JUDGE_API_KEY（或 AR_MODEL_PROVIDER_*，或 ~/.agentruntime/model-provider.env）")
    if not base.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise ValueError("判分模型的地址要是 https（本机测试除外）")
    return JudgeConfig(base_url=base, api_key=key, model=model, embedding_model=embedding_model)


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def run_bridge(
    name: str, python: str, cases: Sequence[Mapping[str, Any]], *, judge: JudgeConfig | None = None, args: Sequence[str] = (),
    timeout_s: float = DEFAULT_TIMEOUT_S, bridges: Path = BRIDGES,
) -> list[dict[str, Any]]:
    """在 python（装了框架的解释器）里跑 bridges/<name>_bridge.py，返回逐条的结果（顺序同 cases）。"""
    script = bridges / f"{name}_bridge.py"
    if not script.is_file():
        raise BridgeError(f"没有这个桥：{name}")
    env = {k: v for k, v in os.environ.items() if k in PASS_THROUGH}
    if judge is not None:
        env.update(judge.env())
    with tempfile.TemporaryDirectory(prefix=f"{name}-bridge-") as tmp:
        src, out = Path(tmp) / "cases.jsonl", Path(tmp) / "scores.jsonl"
        src.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), "utf-8")
        try:
            proc = subprocess.run(
                [python, "-I", str(script), "--cases", str(src), "--out", str(out), *args], env=env, cwd=tmp, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout_s, check=False,
            )
        except FileNotFoundError:
            raise BridgeError(f"找不到 {python}：先按 {REQUIREMENTS.name} 建一个装了 DeepEval 与 Ragas 的 Python") from None
        except subprocess.TimeoutExpired:
            raise BridgeError(f"{name} 的桥超过 {timeout_s:.0f} 秒没跑完") from None
        secret = judge.api_key if judge else ""
        if proc.returncode != 0:
            raise BridgeError(f"{name} 的桥失败（退出码 {proc.returncode}）：{_redact((proc.stderr or proc.stdout).strip()[-1500:], secret)}")
        rows = [json.loads(line) for line in out.read_text("utf-8").splitlines() if line.strip()] if out.exists() else []
    by_id = {str(r.get("id")): r for r in rows}
    missing = [str(c["id"]) for c in cases if str(c["id"]) not in by_id]
    if missing:
        raise BridgeError(f"{name} 的桥少给了 {len(missing)} 条的结果：{', '.join(missing[:5])}")
    return [by_id[str(c["id"])] for c in cases]


__all__ = ["BRIDGES", "REQUIREMENTS", "BridgeError", "JudgeConfig", "judge_config", "run_bridge"]
