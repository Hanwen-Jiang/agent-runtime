from __future__ import annotations

import shutil
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment


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
