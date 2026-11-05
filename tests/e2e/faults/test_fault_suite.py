"""S-07 验收：套件覆盖计划列出的全部 10 个场景，加上 SBX-01 第 6 节的 S11（Sandbox Service 被杀），并进入 CI 的夜间任务。
不需要端到端环境。"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from e2e_support.stack import REPO_ROOT

HERE = Path(__file__).parent
REQUIRED_SCENARIOS = set(range(1, 12))
NIGHTLY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nightly.yml"


def scenario_modules() -> list[Path]:
    """test_fault_s<编号>_*.py：每个场景一个模块。"""
    return sorted(p for p in HERE.glob("test_fault_s*_*.py") if p.stem.removeprefix("test_fault_s").split("_")[0].isdigit())


def _scenarios_of(path: Path) -> dict[str, set[int]]:
    """模块里每个用例覆盖的场景（来自模块级 pytestmark 与用例自己的 scenario 标记）。"""
    spec = importlib.util.spec_from_file_location(f"_s07_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module_marks = getattr(module, "pytestmark", [])
    module_marks = module_marks if isinstance(module_marks, list) else [module_marks]
    out: dict[str, set[int]] = {}
    for name, fn in inspect.getmembers(module, inspect.isfunction):
        if not name.startswith("test_") or fn.__module__ != module.__name__:
            continue
        marks = [*module_marks, *getattr(fn, "pytestmark", [])]
        out[name] = {m.args[0] for m in marks if m.name == "scenario"}
    return out


def test_every_required_scenario_has_tests() -> None:
    covered: set[int] = set()
    for path in scenario_modules():
        for name, scenarios in _scenarios_of(path).items():
            assert scenarios, f"{path.name}::{name} 没有标明覆盖哪个场景"
            covered |= scenarios
    assert covered == REQUIRED_SCENARIOS, f"缺少场景 {sorted(REQUIRED_SCENARIOS - covered)} 的用例"


def test_every_fault_test_is_marked_for_the_nightly_job() -> None:
    assert len(scenario_modules()) == len(REQUIRED_SCENARIOS)
    for path in scenario_modules():
        spec = importlib.util.spec_from_file_location(f"_s07m_{path.stem}", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        marks = {m.name for m in getattr(module, "pytestmark", [])}
        assert {"e2e", "faults"} <= marks, f"{path.name} 要标 e2e 与 faults"


def test_nightly_ci_job_runs_the_fault_suite() -> None:
    if not NIGHTLY_WORKFLOW.exists():
        pytest.fail(f"缺少夜间任务 {NIGHTLY_WORKFLOW.relative_to(REPO_ROOT)}")
    text = NIGHTLY_WORKFLOW.read_text("utf-8")
    assert "schedule:" in text and "cron:" in text, "夜间任务要按计划定时运行"
    assert "tests/e2e" in text and "faults" in text, "夜间任务要跑端到端测试与故障注入套件"
