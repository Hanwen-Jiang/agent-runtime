"""D-14：运行环境（docs/plan/cloud-deploy.md D-14 的验收 2；设计书改动稿 W6 第 1、2 条）的 Worker 一侧。

登记的 images 把运行环境映射到镜像、环境表记运行环境与镜像（后端报了实际的 digest 就以它为准，以前的行不变）都在
Sandbox Service 里（SBX-01，services/go-sandbox-service 的 registry 与 server 测试；K8s 的 imageID 在 libs/go-core 的 k8ssandbox）。
这里看：shell_run 用 AR_SANDBOX_RUNTIME 指定的运行环境，runtime.sandbox span 记着运行环境与服务给的镜像。
"""

from __future__ import annotations

from pathlib import Path

from agentruntime_worker.config import ENV_SANDBOX_RUNTIME, WorkerConfig

from .service_setup import Setup

DIGEST_V1 = "ghcr.io/agentruntime/sandbox-python@sha256:" + "1" * 64


async def test_shell_run_uses_the_configured_runtime_and_records_it(tmp_path: Path) -> None:
    s = Setup(tmp_path, specs={"local": {"kind": "local", "isolation": "none", "image": DIGEST_V1}}, sessions=False, runtime="python")
    await s.py("print(1)", session="")
    [span] = s.spans()
    assert span["sandbox.runtime"] == "python" and span["sandbox.image"] == DIGEST_V1
    assert WorkerConfig.from_env({ENV_SANDBOX_RUNTIME: "universal"}).sandbox_runtime == "universal"
    assert WorkerConfig.from_env({}).sandbox_runtime == "python"
