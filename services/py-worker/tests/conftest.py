from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

# 回放本次会话的全部事件历史要在其他用例之后：它回放的是前面各个用例跑出来的历史。
RUN_LAST = "test_stage_replay.py"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    items.sort(key=lambda item: item.path.name == RUN_LAST)


@pytest.fixture(autouse=True)
def _stop_sandbox_services() -> Iterator[None]:
    """用例里起的 Sandbox Service（SBX-01，sandbox_service_support）在用例结束时停掉，本地后端的 agent 进程一并收掉。"""
    yield
    from .sandbox_service_support import RUNNING

    while RUNNING:
        RUNNING.pop().stop()


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
