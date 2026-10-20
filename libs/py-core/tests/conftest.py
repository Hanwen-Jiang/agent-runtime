import shutil
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

# 一致性套件在包内，需要显式开启断言改写，失败时才有详细信息。
pytest.register_assert_rewrite("agentruntime_core.conformance")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    """Temporal 本地开发服务，整个测试会话共用一个（DurableEngine 一致性测试与 Claim-Check 的 Temporal 用例）。"""
    # 用本机装好的 Temporal CLI，不让 SDK 自行下载。
    temporal = shutil.which("temporal")
    if temporal is None:
        pytest.fail("找不到 temporal 可执行文件（Temporal CLI），见 CLAUDE.md 的常用命令", pytrace=False)
    env = await WorkflowEnvironment.start_local(dev_server_existing_path=temporal)
    try:
        yield env
    finally:
        await env.shutdown()
