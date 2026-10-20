"""py-learning 测试的 fixture；构造评测集的辅助函数在 support.py。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from support import make_set_factory


@pytest.fixture
def make_set(tmp_path: Path) -> Callable[..., Path]:
    """make_set(task_cases, samples=None, catalog=None) → 评测集目录。"""
    return make_set_factory(tmp_path)
