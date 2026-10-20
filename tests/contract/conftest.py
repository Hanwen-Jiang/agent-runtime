"""tests/contract 的公共 fixture。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from contract_tests.paths import GO_TOOLS_DIR
from contract_tests.tools import require_tool, run


@pytest.fixture(scope="session")
def payloadconv_go(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """编译 Go 侧的 payloadconv，整个测试会话只编译一次。"""
    go = require_tool("go")
    exe = "payloadconv.exe" if os.name == "nt" else "payloadconv"
    out = tmp_path_factory.mktemp("bin") / exe
    run([go, "build", "-o", out, "./cmd/payloadconv"], cwd=GO_TOOLS_DIR)
    return out


@pytest.fixture(scope="session")
def claimcheck_go(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """编译 Go 侧的 claimcheck（S-06 跨语言往返），整个测试会话只编译一次。"""
    go = require_tool("go")
    exe = "claimcheck.exe" if os.name == "nt" else "claimcheck"
    out = tmp_path_factory.mktemp("bin") / exe
    run([go, "build", "-o", out, "./cmd/claimcheck"], cwd=GO_TOOLS_DIR)
    return out
