"""DurableEngine 的开发实现：Temporal 适配器 + Temporal 本地开发服务（runtime-quickref 第 6 节）。

适配器与生产实现是同一份代码，只是连的服务不同。开发服务用 PATH 上的 temporal（Temporal CLI）启动。
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentruntime_core.adapters.temporal import TemporalDurableEngineProvider
from agentruntime_core.conformance.durable_engine import DurableEngineConformance
from agentruntime_core.errors import ErrorCategory
from agentruntime_core.testing import FaultInjector

from .temporal_workflows import EchoWorkflow, FailWorkflow

# 开发服务与 Worker 是会话级的，用例要跑在同一个事件循环里。标记也落在同步用例上，
# pytest-asyncio 对此的警告在 pyproject.toml 里过滤掉。
pytestmark = [pytest.mark.temporal, pytest.mark.asyncio(loop_scope="session")]


class _FaultyClient:
    """在真实客户端前加一层故障注入：每个方法调用前先检查 FaultInjector。"""

    def __init__(self, inner: Any, faults: FaultInjector) -> None:
        self._inner = inner
        self._faults = faults

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        async def call(*args: Any, **kwargs: Any) -> Any:
            self._faults.check(name)
            return await target(*args, **kwargs)

        return call


class _FaultyProvider:
    def __init__(self, inner: TemporalDurableEngineProvider, faults: FaultInjector) -> None:
        self._inner = inner
        self._faults = faults

    def bind(self, tenant: Any) -> _FaultyClient:
        return _FaultyClient(self._inner.bind(tenant), self._faults)

    def bind_platform(self, purpose: str) -> _FaultyClient:
        return _FaultyClient(self._inner.bind_platform(purpose), self._faults)

    async def aclose(self) -> None:
        await self._inner.aclose()


class Harness:
    def __init__(self, provider: TemporalDurableEngineProvider, task_queue: str) -> None:
        self.faults = FaultInjector()
        self.provider = _FaultyProvider(provider, self.faults)
        self.task_queue = task_queue

    async def advance(self, delta: timedelta) -> None:
        raise NotImplementedError("DurableEngine 的一致性测试不拨动时间")

    def inject_fault(self, operation: str, category: ErrorCategory) -> None:
        self.faults.fail_next(operation, category)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    # 用本机装好的 Temporal CLI，不让 SDK 自行下载。
    temporal = shutil.which("temporal")
    if temporal is None:
        pytest.fail("找不到 temporal 可执行文件（Temporal CLI），见 CLAUDE.md 的常用命令", pytrace=False)
    env = await WorkflowEnvironment.start_local(dev_server_existing_path=temporal)
    try:
        yield env
    finally:
        await env.shutdown()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def worker_queue(temporal_env: WorkflowEnvironment) -> AsyncIterator[str]:
    task_queue = f"conformance-{uuid.uuid4().hex[:8]}"
    async with Worker(temporal_env.client, task_queue=task_queue, workflows=[EchoWorkflow, FailWorkflow]):
        yield task_queue


class TestTemporalDurableEngine(DurableEngineConformance):
    @pytest.fixture
    def harness(self, temporal_env: WorkflowEnvironment, worker_queue: str) -> Harness:
        return Harness(TemporalDurableEngineProvider(temporal_env.client), worker_queue)
