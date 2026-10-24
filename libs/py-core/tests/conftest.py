import shutil
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment

from agentruntime_core.adapters import milvustest, pgtest, qdranttest

# 一致性套件在包内，需要显式开启断言改写，失败时才有详细信息。
pytest.register_assert_rewrite("agentruntime_core.conformance")


def _missing(reason: str) -> None:
    """缺 Postgres、Qdrant 的二进制或 Docker 时让用例失败；设 AR_ALLOW_MISSING_TOOLS=1 改为跳过。"""
    if pgtest.allow_missing():
        pytest.skip(reason)
    pytest.fail(reason, pytrace=False)


@pytest.fixture(scope="session")
def pg_cluster() -> Iterator[pgtest.Cluster]:
    """临时的 Postgres 实例，整个测试会话共用；每个用例在上面新建自己的库（计划 S-12b）。"""
    try:
        cluster = pgtest.Cluster()
    except pgtest.MissingTool as err:
        _missing(str(err))
    yield cluster
    cluster.stop()


@pytest.fixture(scope="session")
def qdrant_instance() -> Iterator[qdranttest.Instance]:
    """临时的 Qdrant 实例，整个测试会话共用；每个用例用自己的集合前缀（计划 S-12b）。"""
    try:
        instance = qdranttest.Instance()
    except qdranttest.MissingTool as err:
        _missing(str(err))
    yield instance
    instance.stop()


@pytest.fixture(scope="session")
def milvus_instance() -> Iterator[milvustest.Instance]:
    """临时的 Milvus 单机版容器，整个测试会话共用；每个用例用自己的集合前缀（设计改动草稿 C1；T-18）。"""
    try:
        instance = milvustest.Instance()
    except milvustest.MissingTool as err:
        _missing(str(err))
    yield instance
    instance.stop()


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
