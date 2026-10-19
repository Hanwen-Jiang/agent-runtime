from __future__ import annotations

from collections.abc import Iterator

import pytest

from e2e_support.api import TaskApi
from e2e_support.stack import Stack

# 回放导出的事件历史要在所有用例之后：它回放的是前面各个用例跑出来的历史。
RUN_LAST = "test_replay_histories.py"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    items.sort(key=lambda item: item.path.name == RUN_LAST)


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    s = Stack(root=tmp_path_factory.mktemp("e2e"))
    try:
        s.start()
    except Exception:
        diagnostics = s.diagnostics()
        s.stop()
        pytest.fail(f"端到端环境没有起来：\n{diagnostics}", pytrace=False)
    yield s
    s.stop()


@pytest.fixture(scope="session")
def api(stack: Stack) -> TaskApi:
    """租户 t1、用户 u1。"""
    return TaskApi(stack.http_url, stack.token("t1", "u1"))


@pytest.fixture(scope="session")
def other_tenant(stack: Stack) -> TaskApi:
    return TaskApi(stack.http_url, stack.token("t2", "u2"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):  # type: ignore[no-untyped-def]
    outcome = yield
    report = outcome.get_result()
    if report.failed and "stack" in getattr(item, "funcargs", {}):
        report.sections.append(("进程输出", item.funcargs["stack"].diagnostics()))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: 端到端测试，需要 go 与 temporal（Temporal CLI）")
