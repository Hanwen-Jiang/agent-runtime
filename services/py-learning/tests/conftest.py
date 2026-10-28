"""py-learning 测试的 fixture；构造评测集的辅助函数在 support.py。"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from support import make_set_factory


@pytest.fixture
def make_set(tmp_path: Path) -> Callable[..., Path]:
    """make_set(task_cases, samples=None, catalog=None) → 评测集目录。"""
    return make_set_factory(tmp_path)


@pytest.fixture(scope="session")
def redpanda() -> Iterator[object]:
    """临时的 Redpanda 容器，整个测试会话共用；每个用例用自己的主题与消费组。缺 Docker 时失败，AR_ALLOW_MISSING_TOOLS=1 时跳过。"""
    import redpanda_support

    try:
        instance = redpanda_support.Redpanda()
    except redpanda_support.MissingTool as err:
        if os.environ.get("AR_ALLOW_MISSING_TOOLS") == "1":
            pytest.skip(str(err))
        pytest.fail(str(err), pytrace=False)
    yield instance
    instance.stop()
