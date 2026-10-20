"""故障注入与恢复套件（计划 S-07）的 fixture。

整个目录共用端到端环境（tests/e2e/conftest.py 的 stack）。每个用例开始前把 Worker 换成故障 Worker
（e2e_support.fault_worker），目录之外的用例由 tests/e2e/conftest.py 换回标准 Worker，与用例的先后顺序无关。
每个用例开始与结束时清空故障规则，并把环境恢复成健康状态：Worker 活着，task-service 用默认配置运行。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from e2e_support.api import TaskApi
from e2e_support.stack import WRITE_TENANT, Stack


@pytest.fixture(scope="session")
def fstack(stack: Stack) -> Stack:
    return stack


@pytest.fixture(autouse=True)
def _healthy(request: pytest.FixtureRequest) -> Iterator[None]:
    if "fstack" not in request.fixturenames:
        yield
        return
    s: Stack = request.getfixturevalue("fstack")
    s.ensure_healthy()
    s.use_worker("fault")
    yield
    s.ensure_healthy()


@pytest.fixture(scope="session")
def writer(fstack: Stack) -> TaskApi:
    """允许写操作的租户（effect 上限 write_irreversible）。"""
    return TaskApi(fstack.http_url, fstack.token(WRITE_TENANT, "uw"))
