"""镜像里的 Go 模块是单独构建的（deploy/images/go.Dockerfile：GOWORK=off、-mod=readonly），本地开发却用 go.work 的工作区。
一个模块经 libs/go-core 新引入的第三方包没记进它自己的 go.sum 时，本地能编、镜像编不过（2026-11-03 rev21 的 model-gateway：
预算表带进了 modernc.org/sqlite）。这里按镜像的方式对 images.json 里每个 Go 模块各编一次，在构建镜像之前就发现。
要 PATH 上的 go；没有时按 AR_ALLOW_MISSING_TOOLS 的约定跳过或失败。
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from deploy_tests import REPO_ROOT, require_tool

IMAGES = json.loads((REPO_ROOT / "deploy" / "images" / "images.json").read_text("utf-8"))["images"]
GO_MODULES = sorted({v["args"]["MODULE"] for v in IMAGES.values() if v.get("dockerfile") == "deploy/images/go.Dockerfile"})


def test_every_go_image_module_is_listed() -> None:
    assert {"services/go-task-service", "services/go-model-gateway"} <= set(GO_MODULES)


@pytest.mark.parametrize("module", GO_MODULES)
def test_go_module_builds_without_the_workspace(module: str) -> None:
    go = require_tool("go")
    env = {**os.environ, "GOWORK": "off", "GOFLAGS": "-mod=readonly", "CGO_ENABLED": "0"}
    proc = subprocess.run([go, "build", "-o", os.devnull, "./..."], cwd=REPO_ROOT / module, env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"{module} 单独构建失败（go.sum 缺记录时在该模块下跑 GOWORK=off go mod tidy）：\n{proc.stderr[-2000:]}"
