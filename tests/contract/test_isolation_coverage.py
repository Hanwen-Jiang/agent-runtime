"""S-12a 验收“接口层强制租户上下文：S-02 的约定落实到所有实现”与“越权测试套件进入 CI”。

S-02 的一致性测试里，每个 Provider 接口都有越权用例（租户 A 写入 B 读不到、平台与租户互不可见、绑定时复制上下文，
以及各接口自己的跨租户用例）。这里核对它们真的覆盖到每一个接口、每一个实现，以后新增的实现漏跑也会被发现：

- Go：libs/go-core/providers 下每个接口都有 <接口>test 一致性包，包里有越权用例；dev/ 与 adapters/ 下每个实现
  （有 Bind 与 BindPlatform 的包）的测试都跑了对应的一致性包。
- Python：每个一致性测试类都有越权用例；dev/ 与 adapters/ 下每个实现（有 bind_platform 的模块）都有测试类继承它。
- 夜间任务跑越权测试：端到端（Task API 的越权用例）与 Go 的测试（含 Postgres 行级安全，需要 Postgres 二进制）。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import pytest

from contract_tests.paths import REPO_ROOT

GO_CORE = REPO_ROOT / "libs" / "go-core"
PY_CORE = REPO_ROOT / "libs" / "py-core"
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "nightly.yml"

# 不走通用隔离规则（RunIsolationRules）的接口，各自的跨租户用例。
GO_SPECIFIC_ISOLATION = {
    "ingress": "TenantContextComesFromIdentity",
    "telemetry": "TenantAttributesCannotBeForged",
    "modelprovider": "CallsUseTheBoundScope",
}
# Ingress 是确定租户的地方（按认证身份），本身不按租户绑定，没有绑定规则。
GO_UNBOUND = {"ingress"}


def go_sources(directory: Path, *, tests: bool) -> str:
    files = [p for p in directory.glob("*.go") if p.name.endswith("_test.go") == tests]
    return "\n".join(p.read_text("utf-8") for p in files)


def go_interfaces() -> list[str]:
    return sorted(p.name for p in (GO_CORE / "providers").iterdir() if p.is_dir())


def go_implementations() -> list[Path]:
    """dev/ 与 adapters/ 下实现了 Provider 的包：同时有 Bind 与 BindPlatform 方法。"""
    found = []
    for root in (GO_CORE / "dev", GO_CORE / "adapters"):
        for directory in sorted(p for p in root.rglob("*") if p.is_dir()):
            src = go_sources(directory, tests=False)
            if re.search(r"\)\s+Bind\(", src) and re.search(r"\)\s+BindPlatform\(", src):
                found.append(directory)
    return found


@pytest.mark.parametrize("interface", go_interfaces())
def test_every_go_interface_has_cross_tenant_conformance_cases(interface: str) -> None:
    suite = GO_CORE / "conformance" / f"{interface}test"
    assert suite.is_dir(), f"接口 {interface} 缺少一致性测试包 {suite.relative_to(REPO_ROOT)}"
    src = go_sources(suite, tests=False)
    if interface not in GO_UNBOUND:
        assert "RunBindingRules(" in src, f"{interface} 的一致性测试要检查租户绑定规则"
    specific = GO_SPECIFIC_ISOLATION.get(interface)
    if specific:
        assert f'"{specific}"' in src, f"{interface} 的一致性测试缺少跨租户用例 {specific}"
    else:
        assert "RunIsolationRules(" in src, f"{interface} 的一致性测试要跑通用的租户隔离规则"


def test_every_go_implementation_runs_its_conformance_suite() -> None:
    implementations = go_implementations()
    names = {p.relative_to(GO_CORE).as_posix() for p in implementations}
    assert {"dev/sqlitestore", "adapters/postgres", "adapters/temporal"} <= names, f"没找到预期的实现：{sorted(names)}"
    missing = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in implementations
        if not re.search(r"\b(" + "|".join(f"{i}test" for i in go_interfaces()) + r")\.Run\(", go_sources(p, tests=True))
    ]
    assert not missing, f"这些实现没有跑一致性测试（含越权用例）：{missing}"


def python_conformance_classes() -> list[type]:
    import agentruntime_core.conformance as package

    classes = []
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ == module.__name__ and obj.__name__.endswith("Conformance"):
                classes.append(obj)
    return classes


@pytest.mark.parametrize("suite", python_conformance_classes(), ids=lambda c: c.__name__)
def test_every_python_conformance_suite_has_cross_tenant_cases(suite: type) -> None:
    from agentruntime_core.conformance.common import BindingRules, IsolationRules

    assert issubclass(suite, BindingRules), f"{suite.__name__} 要检查租户绑定规则"
    tenant_cases = [name for name in dir(suite) if name.startswith("test_") and ("tenant" in name or "scope" in name)]
    assert issubclass(suite, IsolationRules) or tenant_cases, f"{suite.__name__} 缺少跨租户用例"


def test_every_python_implementation_runs_a_conformance_suite() -> None:
    src_root = PY_CORE / "src" / "agentruntime_core"
    modules = [
        p for p in [*(src_root / "dev").glob("*.py"), *(src_root / "adapters").glob("*.py")]
        if not p.name.startswith("_") and "def bind_platform(" in p.read_text("utf-8")
    ]
    assert len(modules) >= 10, f"没找到预期数量的实现：{[m.name for m in modules]}"
    test_files = {p: p.read_text("utf-8") for p in (PY_CORE / "tests").rglob("test_*.py")}
    missing = []
    for module in modules:
        dotted = ".".join(module.relative_to(PY_CORE / "src").with_suffix("").parts)
        # 引用了这个模块的测试文件里，要有继承一致性测试类的测试类。
        users = [text for text in test_files.values() if dotted in text]
        if not any(re.search(r"class Test\w+\(\w+Conformance\)", text) for text in users):
            missing.append(dotted)
    assert not missing, f"这些实现没有跑一致性测试（含越权用例）：{missing}"


def test_nightly_job_runs_the_cross_tenant_suites() -> None:
    text = NIGHTLY.read_text("utf-8")
    assert "tests/e2e" in text, "夜间任务要跑端到端的越权用例"
    assert re.search(r"(?i)postgres", text) and "go test" in text, "夜间任务要跑 Go 的测试（含 Postgres 行级安全），并装好 Postgres"


def test_nightly_job_runs_the_memory_cross_tenant_suites_on_postgres_and_qdrant() -> None:
    """S-12b：Python 侧的 Postgres、Qdrant 适配器与 Memory Service 的越权用例进入 CI，装好两种二进制、不允许跳过。"""
    text = NIGHTLY.read_text("utf-8")
    assert "libs/py-core" in text and "services/py-memory" in text, "夜间任务要跑 py-core 适配器与 Memory Service 的测试"
    assert re.search(r"(?i)qdrant", text), "夜间任务要装好 Qdrant"
    assert "AR_ALLOW_MISSING_TOOLS: \"1\"" not in text and "AR_ALLOW_MISSING_TOOLS=1" not in text, "越权用例不允许因为缺二进制而跳过"
