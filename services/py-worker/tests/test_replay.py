"""S-03 验收：重放（计划 S-03 验收“重放”，硬门槛）。

- tests/histories 里是集成测试导出的 Task 工作流事件历史（tests/e2e 导出，见 CLAUDE.md 常用命令），
  当前代码必须都能回放。
- 把 Task 工作流里两条标了 replay-mutation 的语句调换顺序（收尾写回与通知 Session），重放必须失败，
  证明重放测试确实能发现命令顺序的改变。对照组：不调换、用同样方式加载的代码能回放，失败不是加载方式造成的。
"""

from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner
from temporalio.workflow import NondeterminismError

import agentruntime_worker.workflows.task as task_module
from agentruntime_worker.replay import load_histories, main, replay_histories

from .support import HISTORIES_DIR

MUTATION_A = "# replay-mutation:A"
MUTATION_B = "# replay-mutation:B"


def golden_files() -> list[Path]:
    files = sorted(HISTORIES_DIR.glob("*.json"))
    if len(files) < 2:
        pytest.fail(f"{HISTORIES_DIR} 下至少要有 2 份事件历史（完成、取消），实际 {len(files)} 份；用 tests/e2e 导出", pytrace=False)
    return files


def test_golden_histories_cover_completion_and_cancellation() -> None:
    texts = [f.read_text("utf-8") for f in golden_files()]
    assert any(re.search(r"(?i)workflow_?execution_?completed", t) for t in texts), "缺少正常完成的运行"
    assert any(re.search(r"(?i)workflow_?execution_?cancel_?requested", t) for t in texts), "缺少被取消的运行"
    assert any(re.search(r"(?i)signal_?external", t) for t in texts), "缺少通知 Session 工作流的运行"


async def test_golden_histories_replay() -> None:
    histories = load_histories(HISTORIES_DIR)
    assert len(histories) == len(golden_files())
    results = await replay_histories(histories)
    failures = [(h.workflow_id, r.replay_failure) for h, r in zip(histories, results, strict=True) if r.replay_failure]
    assert not failures, failures


def load_variant(source: str, tmp_path: Path) -> ModuleType:
    name = f"task_variant_{uuid.uuid4().hex}"
    path = tmp_path / f"{name}.py"
    path.write_text(source, "utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def swap_marked_lines(source: str) -> str:
    lines = source.split("\n")
    a = [i for i, line in enumerate(lines) if MUTATION_A in line]
    b = [i for i, line in enumerate(lines) if MUTATION_B in line]
    assert len(a) == 1 and len(b) == 1, f"task.py 里 {MUTATION_A} 与 {MUTATION_B} 应各出现一次"
    lines[a[0]], lines[b[0]] = lines[b[0]], lines[a[0]]
    return "\n".join(lines)


async def replay_with(module: ModuleType) -> list[BaseException | None]:
    replayer = Replayer(workflows=[module.TaskWorkflow], workflow_runner=UnsandboxedWorkflowRunner())
    histories = load_histories(HISTORIES_DIR)
    out: list[BaseException | None] = []
    for history in histories:
        result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
        out.append(result.replay_failure)
    return out


async def test_unmodified_source_loaded_the_same_way_replays(tmp_path: Path) -> None:
    golden_files()
    source = Path(task_module.__file__).read_text("utf-8")
    failures = await replay_with(load_variant(source, tmp_path))
    assert failures and not any(failures), failures


async def test_replay_detects_reordered_commands(tmp_path: Path) -> None:
    golden_files()
    source = Path(task_module.__file__).read_text("utf-8")
    failures = await replay_with(load_variant(swap_marked_lines(source), tmp_path))
    assert any(isinstance(f, NondeterminismError) for f in failures), f"调换命令顺序后重放仍然通过，重放测试无效：{failures}"


def test_replay_cli(tmp_path: Path) -> None:
    golden_files()
    assert main(["--dir", str(HISTORIES_DIR)]) == 0
    assert main(["--dir", str(tmp_path)]) == 2, "目录里没有事件历史时不能当作回放通过"
