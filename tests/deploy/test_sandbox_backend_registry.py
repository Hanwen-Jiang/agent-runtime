"""D-19 验收（docs/plan/firecracker-backend.md 第 5 节）：Chart 里沙箱后端登记的启用开关与能力。

云上的登记里有 firecracker（kind firecracker、存档模式、能力 snapshot/block_attach/vsock），但 enabled: false（D-20 之前没有实现）。
登记写错时 Chart 渲染就失败，与 Worker 加载登记时的校验一致：default 指向停用的后端、能力取值不认识、firecracker 不是存档模式、
firecracker 设成启用、enabled 不是布尔值。
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


def test_cloud_registers_firecracker_disabled() -> None:
    backends = registry(render(CLOUD_VALUES))["backends"]
    fc = backends["firecracker"]
    assert fc["kind"] == "firecracker" and fc["isolation"] == "microvm" and fc["workspace"] == "archive"
    assert fc["enabled"] is False
    assert sorted(fc["capabilities"]) == ["block_attach", "snapshot", "vsock"]
    assert all(b.get("enabled", True) for name, b in backends.items() if name != "firecracker"), "现有后端照旧启用"


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
        (["worker.shell.backends.firecracker.enabled=true"], "D-20"),
        (["worker.shell.backends.gvisor.capabilities={teleport}"], "capabilities"),
        (["worker.shell.backends.gvisor.enabled=no"], "enabled"),
    ],
)
def test_bad_registry_fails_to_render(set_values: list[str], why: str) -> None:
    assert why in render_fails(*set_values)
