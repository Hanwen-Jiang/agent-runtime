"""D-05、D-08：shell_run 经 Sandbox 跑（AR_TOOL_SHELL_SANDBOX=sandbox；docs/plan/cloud-deploy.md）。SBX-01 之后经 Sandbox Service，
这里对着真的服务进程（service_setup）看 Worker 一侧：

- 每条命令一个绑定本 workspace 的一次性环境：看得到工作区里的文件，写的文件留在工作区；拿不到 Worker 的环境变量；超时返回 timed_out。
- 用哪个后端由租户策略的 sandbox.backend 决定（服务按策略选），没写用 default；策略要的后端没登记时拒绝，不悄悄换。结果里写着用的哪个后端。
- 配置：sandbox 时必须给 Sandbox Service 的地址（后端登记归服务，格式的核对在 services/go-sandbox-service 的 registry 测试里）。
（K8s 后端在 kind 里的工作区共享在 libs/go-core 的 k8ssandbox 测试里。）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.errors import InvalidError

from agentruntime_worker.config import ENV_SANDBOX_SERVICE_ADDRESS, ENV_TOOL_SHELL_SANDBOX, WorkerConfig
from agentruntime_worker.tools.platform import PlatformToolsConfig, parse_cpu_millis, parse_memory_mb, register_platform_tools

from .service_setup import LOCAL, Setup

BACKENDS = {"plain": LOCAL, "other": LOCAL}


def make(tmp_path: Path, policies: dict[str, Any] | None = None) -> Setup:
    s = Setup(tmp_path, specs=BACKENDS, default="plain", sessions=False, policies=policies)
    (s.host / "t1" / "w1").mkdir(parents=True, exist_ok=True)
    (s.host / "t1" / "w1" / "seed.txt").write_text("种子", "utf-8")
    return s


async def test_shell_run_goes_through_the_sandbox_bound_to_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AR_TEST_WORKER_SECRET", "worker-only-secret-9c1d")
    s = make(tmp_path)
    code = (
        "import os; print(open('seed.txt', encoding='utf-8').read()); print(os.environ.get('AR_TEST_WORKER_SECRET', 'absent'));"
        "open('made.txt', 'w').write('x')"
    )
    out = await s.py(code, session="")
    assert out["exit_code"] == 0 and out["sandbox"] == "plain"
    assert "种子" in out["stdout"] and "absent" in out["stdout"], out
    assert (s.host / "t1" / "w1" / "made.txt").read_text() == "x", "写的文件留在工作区"


async def test_shell_run_in_the_sandbox_times_out(tmp_path: Path) -> None:
    s = make(tmp_path)
    out = await s.call("shell_run", {"argv": ["python", "-c", "import time; time.sleep(30)"], "timeout_s": 1}, session="")
    assert out["timed_out"] and out["exit_code"] is None


async def test_tenant_policy_picks_the_backend(tmp_path: Path) -> None:
    s = make(tmp_path, {"t2": {"sandbox": {"backend": "other"}}})
    assert (await s.py("print(1)", session=""))["sandbox"] == "plain", "没写策略用 default"
    assert (await s.py("print(1)", tenant_id="t2", session=""))["sandbox"] == "other"


async def test_each_run_is_metered_in_a_span_without_the_command_or_output(tmp_path: Path) -> None:
    """D-07、D-10：一次运行一个 runtime.sandbox span，记后端、隔离强度、存活与执行时间；不记命令与输出。"""
    s = make(tmp_path)
    await s.py("print('secret-output-7f3a')", session="")
    [attrs] = s.spans()
    assert attrs["sandbox.backend"] == "plain" and attrs["sandbox.isolation"] == "none" and attrs["sandbox.kind"] == "local"
    assert attrs["tenant_id"] == "t1" and attrs["gen_ai.tool.name"] == "shell_run"
    assert attrs["sandbox.lifetime_ms"] >= attrs["sandbox.exec_ms"] >= 0 and attrs["sandbox.timed_out"] is False
    assert "secret-output-7f3a" not in json.dumps(attrs, ensure_ascii=False, default=str), "span 里不记命令与输出"


async def test_unknown_backend_in_policy_is_rejected(tmp_path: Path) -> None:
    s = make(tmp_path, {"t1": {"sandbox": {"backend": "firecracker-x"}}})
    with pytest.raises(InvalidError) as info:
        await s.py("print(1)", session="")
    assert info.value.code == "unknown_sandbox_backend"


def test_sandbox_mode_needs_the_sandbox_service(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=ENV_SANDBOX_SERVICE_ADDRESS):
        WorkerConfig.from_env({ENV_TOOL_SHELL_SANDBOX: "sandbox"})
    config = WorkerConfig.from_env({ENV_TOOL_SHELL_SANDBOX: "sandbox", ENV_SANDBOX_SERVICE_ADDRESS: "127.0.0.1:8097"})
    assert config.sandbox_service_address == "127.0.0.1:8097"
    with pytest.raises(ValueError, match=ENV_SANDBOX_SERVICE_ADDRESS):
        register_platform_tools(LocalToolDriverProvider(), PlatformToolsConfig(workspace_root=tmp_path, shell_sandbox="sandbox"))


def test_docker_style_limits_are_converted() -> None:
    assert parse_cpu_millis("2") == 2000 and parse_cpu_millis("0.5") == 500
    assert parse_memory_mb("2g") == 2048 and parse_memory_mb("512m") == 512


def test_worker_config_has_no_platform_credentials() -> None:
    """包镜像的地址与环境凭据都在服务里（SBX-01）：Worker 的配置不再有包镜像。"""
    assert not hasattr(WorkerConfig.from_env({}), "pkg_mirror_url")
