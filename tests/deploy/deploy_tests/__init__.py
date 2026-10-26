"""T-15 部署验收的辅助函数：仓库路径、外部工具（helm、otelcol-contrib）、渲染 Chart。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml


def _find_repo_root() -> Path:
    env = os.environ.get("AR_REPO_ROOT")
    if env:
        return Path(env).resolve()
    for start in (Path(__file__).resolve(), Path.cwd().resolve()):
        for candidate in (start, *start.parents):
            if (candidate / "buf.yaml").is_file() and (candidate / "deploy").is_dir():
                return candidate
    raise RuntimeError("找不到仓库根目录（含 buf.yaml 与 deploy/ 的目录）；可设置 AR_REPO_ROOT")


REPO_ROOT = _find_repo_root()
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "agent-runtime"
COLLECTOR_CONFIG = CHART_DIR / "files" / "otel-collector.yaml"
PRODUCTION_VALUES = CHART_DIR / "values-production.example.yaml"
# 云上单节点（docs/plan/cloud-deploy.md D-04）。
CLOUD_VALUES = CHART_DIR / "values-cloud.yaml"
RELEASE = "ar"


def require_tool(name: str) -> str:
    """返回工具的路径；找不到时测试失败。本地临时缺工具时可设 AR_ALLOW_MISSING_TOOLS=1 改为跳过。"""
    path = shutil.which(name)
    if path:
        return path
    message = f"{name} 不在 PATH 上；T-15 的部署测试需要它"
    if os.environ.get("AR_ALLOW_MISSING_TOOLS") == "1":
        pytest.skip(message)
    pytest.fail(message, pytrace=False)


def run(cmd: Sequence[str | os.PathLike[str]], *, cwd: Path = REPO_ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    args = [os.fspath(part) for part in cmd]
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        pytest.fail(
            f"命令失败（退出码 {proc.returncode}）：{' '.join(args)}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
            pytrace=False,
        )
    return proc


def render(*values_files: Path, set_values: Sequence[str] = ()) -> list[dict[str, Any]]:
    """helm template 渲染 Chart，返回全部清单（去掉空文档）。"""
    helm = require_tool("helm")
    cmd: list[str | os.PathLike[str]] = [helm, "template", RELEASE, CHART_DIR, "--namespace", "agent-runtime"]
    for f in values_files:
        cmd += ["--values", f]
    for s in set_values:
        cmd += ["--set", s]
    out = run(cmd).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def by_kind(manifests: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [m for m in manifests if m.get("kind") == kind]


def pod_spec(manifest: dict[str, Any]) -> dict[str, Any]:
    """Deployment、Job、CronJob、ScaledJob 的 Pod 模板。"""
    kind = manifest["kind"]
    if kind == "ScaledJob":
        return manifest["spec"]["jobTargetRef"]["template"]["spec"]
    if kind == "CronJob":
        return manifest["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return manifest["spec"]["template"]["spec"]


WORKLOAD_KINDS = ("Deployment", "StatefulSet", "Job", "CronJob", "ScaledJob")


def containers(manifests: list[dict[str, Any]]) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """(工作负载, 容器)，含 initContainers。"""
    for m in manifests:
        if m.get("kind") in WORKLOAD_KINDS:
            spec = pod_spec(m)
            for c in [*spec.get("initContainers", []), *spec.get("containers", [])]:
                yield m, c


def env_of(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in container.get("env", [])}


def port_of(address: str) -> int:
    match = re.search(r":(\d+)$", address)
    assert match, f"地址里没有端口：{address}"
    return int(match.group(1))
