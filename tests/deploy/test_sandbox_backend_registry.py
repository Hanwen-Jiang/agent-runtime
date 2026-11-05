"""D-19 验收（docs/plan/firecracker-backend.md 第 5 节）：Chart 里沙箱后端登记的启用开关与能力。

云上的登记里有 firecracker（kind firecracker、存档模式、启用，经宿主上的 fc-manager，D-20）。
登记写错时 Chart 渲染就失败，与 Worker 加载登记时的校验一致：default 指向停用的后端、能力取值不认识、firecracker 不是存档模式、
firecracker 启用却没写 manager_url、enabled 不是布尔值。
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from deploy_tests import CHART_DIR, CLOUD_VALUES, RELEASE, by_kind, render, require_tool, run


def registry(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    [cm] = [m for m in by_kind(manifests, "ConfigMap") if m["metadata"]["name"].endswith("-sandbox-backends")]
    return json.loads(cm["data"]["backends.json"])


def test_cloud_registers_firecracker() -> None:
    reg = registry(render(CLOUD_VALUES))
    fc = reg["backends"]["firecracker"]
    assert fc["kind"] == "firecracker" and fc["isolation"] == "microvm" and fc["workspace"] == "archive"
    assert fc["enabled"] is True and fc["manager_url"].startswith("http://10.42.0.1:")
    assert reg["default"] != "firecracker", "先只给租户策略指到它的租户用"
    assert all(b.get("enabled", True) for b in reg["backends"].values()), "后端都启用"


def render_fails(*set_values: str) -> str:
    helm = require_tool("helm")
    cmd: list[str | os.PathLike[str]] = [helm, "template", RELEASE, CHART_DIR, "--namespace", "agent-runtime", "--values", CLOUD_VALUES]
    for s in set_values:
        cmd += ["--set", s]
    proc = run(cmd, check=False)
    assert proc.returncode != 0, f"应当渲染失败：{set_values}"
    return proc.stderr


@pytest.mark.parametrize(
    ("set_values", "why"),
    [
        (["worker.shell.backends.gvisor.enabled=false"], "worker.shell.defaultBackend"),
        (["worker.shell.backends.firecracker.workspace=mount"], "archive"),
        (["worker.shell.backends.firecracker.manager_url="], "manager_url"),
        (["worker.shell.backends.gvisor.capabilities={teleport}"], "capabilities"),
        (["worker.shell.backends.gvisor.enabled=no"], "enabled"),
    ],
)
def test_bad_registry_fails_to_render(set_values: list[str], why: str) -> None:
    assert why in render_fails(*set_values)
