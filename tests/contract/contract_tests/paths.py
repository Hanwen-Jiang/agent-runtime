"""仓库内的固定路径。"""

from __future__ import annotations

import os
from pathlib import Path


def _find_repo_root() -> Path:
    env = os.environ.get("AR_REPO_ROOT")
    if env:
        return Path(env).resolve()
    for start in (Path(__file__).resolve(), Path.cwd().resolve()):
        for candidate in (start, *start.parents):
            if (candidate / "buf.yaml").is_file() and (candidate / "schemas").is_dir():
                return candidate
    raise RuntimeError("找不到仓库根目录（含 buf.yaml 与 schemas/ 的目录）；可设置 AR_REPO_ROOT")


REPO_ROOT = _find_repo_root()
PROTO_DIR = REPO_ROOT / "proto"
SCHEMAS_DIR = REPO_ROOT / "schemas"
EXAMPLES_DIR = SCHEMAS_DIR / "examples"
CONTRACT_TESTS_DIR = REPO_ROOT / "tests" / "contract"
FIXTURES_DIR = CONTRACT_TESTS_DIR / "fixtures"
GO_TOOLS_DIR = CONTRACT_TESTS_DIR / "go"
