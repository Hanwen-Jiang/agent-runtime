from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from agentruntime_core.adapters import pgtest, qdranttest

from .memory_support import Backend, make_backend


@pytest.fixture
async def dev(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    """开发后端：SQLite + 内存 VectorStore。"""
    async for b in make_backend("dev", tmp_path, request):
        yield b


@pytest.fixture(params=["dev", pytest.param("prod", marks=pytest.mark.backends)])
async def backend(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    """两套后端都跑：开发后端，与 Postgres（行级安全）+ Qdrant（租户分区、强制过滤、保距变换）。"""
    async for b in make_backend(request.param, tmp_path, request):
        yield b


def _missing(reason: str) -> None:
    """缺 Postgres 或 Qdrant 的二进制时让用例失败；设 AR_ALLOW_MISSING_TOOLS=1 改为跳过。"""
    if pgtest.allow_missing():
        pytest.skip(reason)
    pytest.fail(reason, pytrace=False)


@pytest.fixture(scope="session")
def pg_cluster() -> Iterator[pgtest.Cluster]:
    try:
        cluster = pgtest.Cluster()
    except pgtest.MissingTool as err:
        _missing(str(err))
    yield cluster
    cluster.stop()


@pytest.fixture(scope="session")
def qdrant_instance() -> Iterator[qdranttest.Instance]:
    try:
        instance = qdranttest.Instance()
    except qdranttest.MissingTool as err:
        _missing(str(err))
    yield instance
    instance.stop()
