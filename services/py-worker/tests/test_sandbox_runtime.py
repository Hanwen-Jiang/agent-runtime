"""D-14：运行环境（docs/plan/cloud-deploy.md D-14 的验收 2；设计书改动稿 W6 第 1、2 条）。

- 后端登记的 images 把运行环境（runtime）映射到镜像；只给 image 时就是 python 的镜像（D-05 的写法）。
- 环境表与 runtime.sandbox span 记着运行环境与镜像：后端报了实际的镜像（K8s 的 imageID，按 digest）就以它为准，
  否则记登记里配的那个。换了镜像版本以后，以前的行不变。
- shell_run 用 AR_SANDBOX_RUNTIME 指定的运行环境。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from agentruntime_core.adapters.k8s_sandbox import _image_id
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.providers.sandbox import SandboxEnv, SandboxSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.config import ENV_SANDBOX_RUNTIME, WorkerConfig
from agentruntime_worker.sandbox.broker import SandboxBroker
from agentruntime_worker.sandbox.ledger import SandboxLedger
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.tools.sandboxes import Sandboxes

T1 = TenantContext(tenant_id="t1", user_id="u1", workspace_id="w1")
DIGEST_V1 = "ghcr.io/agentruntime/sandbox-python@sha256:" + "1" * 64
DIGEST_V2 = "ghcr.io/agentruntime/sandbox-python@sha256:" + "2" * 64


def make(tmp_path: Path, image: str) -> tuple[SandboxBroker, SandboxLedger]:
    (tmp_path / "workspaces" / "t1" / "w1").mkdir(parents=True, exist_ok=True)
    boxes = Sandboxes({"local": {"kind": "local", "isolation": "none", "image": image}}, "local", workspace_root=tmp_path / "workspaces")
    ledger = SandboxLedger(SQLiteRelationalStoreProvider(tmp_path / "relational"))
    return SandboxBroker(boxes, ledger), ledger


def test_images_map_runtimes_and_image_is_the_python_one() -> None:
    boxes = Sandboxes({
        "k": {"kind": "k8s", "isolation": "container", "image": "py:1", "images": {"universal": "uni:1", "node": "node:1"}},
        "old": {"kind": "k8s", "isolation": "container", "image": "py:0"},
    }, "k")
    assert boxes.image_for("k", "universal") == "uni:1" and boxes.image_for("k", "python") == "py:1"
    assert boxes.image_for("old", "python") == "py:0" and boxes.image_for("old", "universal") == ""


async def test_configured_image_is_recorded_and_old_rows_keep_theirs(tmp_path: Path) -> None:
    broker, ledger = make(tmp_path, DIGEST_V1)
    first = await broker.acquire(T1, SandboxSpec())
    assert first.record is not None and first.record.image == DIGEST_V1 and first.record.runtime == "python"
    await broker.release(first)
    broker2, _ = make(tmp_path, DIGEST_V2)  # 换了镜像版本（同一个业务库）
    second = await broker2.acquire(T1, SandboxSpec())
    assert second.record is not None and second.record.image == DIGEST_V2
    assert (await ledger.get("t1", first.record.env_ref)).image == DIGEST_V1, "以前的行不变"  # type: ignore[union-attr]


async def test_the_image_reported_by_the_backend_wins(tmp_path: Path, monkeypatch: Any) -> None:
    broker, ledger = make(tmp_path, "ghcr.io/agentruntime/sandbox-python:0.1.0")
    provider = (await broker.sandboxes.backend("local")).provider
    box_class = type(provider.bind(T1))
    real = box_class.create

    async def create(self: Any, spec: SandboxSpec) -> SandboxEnv:
        env = await real(self, spec)
        return SandboxEnv(env_id=env.env_id, spec=env.spec, image=DIGEST_V1)

    monkeypatch.setattr(box_class, "create", create)
    lease = await broker.acquire(T1, SandboxSpec())
    assert lease.record is not None and lease.record.image == DIGEST_V1, "标签换成后端报的 digest"
    assert lease.attributes["sandbox.image"] == DIGEST_V1 and lease.attributes["sandbox.runtime"] == "python"


def test_k8s_reports_the_image_id_of_the_sandbox_container() -> None:
    pod = {"status": {"containerStatuses": [
        {"name": "other", "imageID": "x@sha256:" + "0" * 64},
        {"name": "sandbox", "imageID": "docker-pullable://" + DIGEST_V1},
    ]}}
    assert _image_id(pod) == DIGEST_V1
    assert _image_id({"status": {}}) == ""


async def test_shell_run_uses_the_configured_runtime_and_records_it(tmp_path: Path) -> None:
    broker, _ = make(tmp_path, DIGEST_V1)
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
    tools = LocalToolDriverProvider()
    register_platform_tools(tools, PlatformToolsConfig(workspace_root=tmp_path / "workspaces", shell_sandbox="sandbox", shell_runtime="python"),
                            sandboxes=broker, telemetry=telemetry)
    await tools.bind(T1).invoke("shell_run", "v1", {"argv": ["python", "-c", "print(1)"]}, idempotency_key="idk-rt")
    await telemetry.bind(T1).flush()
    [span] = [s for s in telemetry.exported_spans if s.name == "runtime.sandbox"]
    assert span.attributes["sandbox.runtime"] == "python" and span.attributes["sandbox.image"] == DIGEST_V1
    assert WorkerConfig.from_env({ENV_SANDBOX_RUNTIME: "universal"}).sandbox_runtime == "universal"
    assert WorkerConfig.from_env({}).sandbox_runtime == "python"
