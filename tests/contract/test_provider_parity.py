"""两种语言都有的 Provider 接口方法一一对应，并在文件头注明（计划 S-02 交付“接口定义”）。

Python 的 Protocol 与 Go 的 interface 按名字比对：Go 的 CamelCase 转成 snake_case 后应与 Python 相同。
惯用法不同的地方列在 IDIOMS 里，例如 Python 的 aclose 对应 Go 的 Close。
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from functools import cache
from pathlib import Path

import pytest

from contract_tests.paths import GO_TOOLS_DIR, REPO_ROOT
from contract_tests.tools import require_tool, run

GO_CORE = REPO_ROOT / "libs" / "go-core"

# Python 类（模块.类名）→（Go 包目录，Go 接口名）。
SHARED = {
    "cache.Cache": ("providers/cache", "Cache"),
    "cache.CacheProvider": ("providers/cache", "Provider"),
    "object_store.ObjectStore": ("providers/objectstore", "Store"),
    "object_store.ObjectStoreProvider": ("providers/objectstore", "Provider"),
    "secret_store.SecretStore": ("providers/secretstore", "Store"),
    "secret_store.SecretStoreProvider": ("providers/secretstore", "Provider"),
    "telemetry.Telemetry": ("providers/telemetry", "Telemetry"),
    "telemetry.Span": ("providers/telemetry", "Span"),
    "telemetry.TelemetryProvider": ("providers/telemetry", "Provider"),
    "durable_engine.DurableEngine": ("providers/durable", "Engine"),
    "durable_engine.DurableEngineProvider": ("providers/durable", "Provider"),
    "relational_store.RelationalStore": ("providers/relational", "Store"),
    "relational_store.Transaction": ("providers/relational", "Tx"),
    "relational_store.RelationalStoreProvider": ("providers/relational", "Provider"),
}

# Python 名 → 对应的 Go 名（snake_case 后）。
IDIOMS = {"aclose": "close"}
# 只在 Go 侧有的方法：Go 用 defer span.End()，Python 用 async with 自动结束 span。
GO_ONLY = {("providers/telemetry", "Span"): {"end"}}


def snake(go_name: str) -> str:
    """TemporaryURL → temporary_url，TraceID → trace_id，SetIfAbsent → set_if_absent。"""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", go_name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def python_members(qualified: str) -> set[str]:
    module_name, class_name = qualified.split(".")
    cls = getattr(importlib.import_module(f"agentruntime_core.providers.{module_name}"), class_name)
    members: set[str] = set()
    for klass in inspect.getmro(cls):
        if klass.__module__.startswith("agentruntime_core."):
            members |= {
                name
                for name, value in vars(klass).items()
                if not name.startswith("_") and (callable(value) or isinstance(value, property))
            }
    return {IDIOMS.get(name, name) for name in members}


@cache
def go_interfaces() -> dict[str, dict[str, list[str]]]:
    go = require_tool("go")
    dirs = sorted({d for d, _ in SHARED.values()})
    proc = run([go, "run", "./cmd/ifacedump", *[str(GO_CORE / d) for d in dirs]], cwd=GO_TOOLS_DIR)
    return {
        Path(directory).resolve().relative_to(GO_CORE.resolve()).as_posix(): interfaces
        for directory, interfaces in json.loads(proc.stdout).items()
    }


@pytest.mark.parametrize("qualified", sorted(SHARED))
def test_shared_interfaces_have_the_same_methods(qualified: str) -> None:
    go_dir, go_name = SHARED[qualified]
    interfaces = go_interfaces().get(go_dir, {})
    assert go_name in interfaces, f"Go 侧缺少 {go_dir}.{go_name}"
    go_methods = {snake(m) for m in interfaces[go_name]} - GO_ONLY.get((go_dir, go_name), set())
    py_methods = python_members(qualified)
    assert py_methods == go_methods, (
        f"{qualified} 与 {go_dir}.{go_name} 不一致：只在 Python {sorted(py_methods - go_methods)}，"
        f"只在 Go {sorted(go_methods - py_methods)}"
    )


@pytest.mark.parametrize("module_name", sorted({q.split(".")[0] for q in SHARED}))
def test_shared_interfaces_say_so_in_both_file_headers(module_name: str) -> None:
    py_doc = importlib.import_module(f"agentruntime_core.providers.{module_name}").__doc__ or ""
    assert "两种语言共用" in py_doc, f"{module_name}.py 的文件头没有注明两种语言共用"
    go_dir = next(d for q, (d, _) in SHARED.items() if q.startswith(f"{module_name}."))
    go_files = [p for p in (GO_CORE / go_dir).glob("*.go") if not p.name.endswith("_test.go")]
    assert any("两种语言共用" in p.read_text("utf-8") for p in go_files), f"{go_dir} 的文件头没有注明两种语言共用"
