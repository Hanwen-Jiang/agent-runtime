"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 2 节的代码侧防护（线上诊断由人执行，另记）。

细化见该计划第 11 节：
- Workspace API 建完核对：从别的工作区复制之后，新工作区里一个文件也没有、而源有文件时，报 workspace_empty_after_copy，
  不留空的副本（行与环境都撤掉），源不受影响。源本来就是空的照常复制。
- Agent 侧早停：tool_loop 里 repo_list 列工作区根（path 为空、"." 或 "/"）得到空目录时，这个 Node 的结果带硬信号 environment
  （附录 B.6 的扩展，见 defs.schema.json 的 hard_signal）。Reviewer 的 floor 见到它只允许 done（交付“工作区是空的”这类答复）、
  问人或收尾，不再 continue、repair、replan；第一轮就会问人，问题带 question_code workspace_empty。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime_core.dev import workdir

from agentruntime_worker.activities.types import ToolLoopStep

from .reviewer_support import atomic as r_atomic
from .reviewer_support import done as r_done
from .reviewer_support import goal as r_goal
from .reviewer_support import ok, replan
from .reviewer_support import stage as r_stage
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .test_reviewer_rules import decide
from .test_stage_runner import deliver, goal, one_round, results, scripted_steps
from .test_workspace_operations import Op, Setup, make_remote

# --- Workspace API：复制后核对 ---


@pytest.mark.parametrize("mode", ["archive", "mount"])
async def test_a_copy_that_comes_out_empty_is_refused_and_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    url, _ = make_remote(tmp_path)
    s = Setup(tmp_path, mode=mode)
    await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace="w-base", repo_url=url)
    # 假设 2：只读的环境里看不到源的文件，导出是空的（存档模式下源还没有快照时照旧经环境复制）；挂卷时在卷上复制出来是空的
    # （N-10 之后挂卷的复制不经环境）。
    from agentruntime_worker.workspace import operations
    from agentruntime_worker.workspace.copy import CopyStats

    empty = tmp_path / "nothing"
    empty.mkdir()
    real = workdir.export_dir
    monkeypatch.setattr(workdir, "export_dir", lambda root, out, *, max_bytes: real(empty, out, max_bytes=max_bytes))

    async def no_snapshot(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(s.archiver, "copy_snapshot", no_snapshot)
    monkeypatch.setattr(operations, "copy_working_copy", lambda src, dst, *, max_bytes: CopyStats())
    err = await s.error(Op.WORKSPACE_OPERATION_CREATE, workspace="w-copy", from_workspace_id="w-base")
    assert err.code == "workspace_empty_after_copy"
    left = await s.archiver.get("t1", "w-copy")
    assert left is None or left.state == "deleted", "不留空的副本"
    monkeypatch.setattr(workdir, "export_dir", real)
    entries = (await s.run(Op.WORKSPACE_OPERATION_LIST_FILES, workspace="w-base"))["entries"]
    assert any(e.get("path") == "README.md" or e.get("name") == "README.md" for e in entries), "源不受影响"


async def test_copying_an_empty_workspace_is_fine(tmp_path: Path) -> None:
    s = Setup(tmp_path, mode="archive")
    await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace="w-empty")
    await s.run(Op.WORKSPACE_OPERATION_CREATE, workspace="w-copy", from_workspace_id="w-empty")
    assert await s.archiver.get("t1", "w-copy") is not None, "源本来就是空的：照常复制"


# --- tool_loop：列工作区根得到空目录 ---


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("root", ["", ".", "/"])
async def test_listing_an_empty_workspace_root_marks_the_environment_signal(harness: StageHarness, root: str) -> None:
    s = harness.script
    s.tools["repo_list"] = "read"
    s.tool_results["repo_list"] = lambda i: {"path": ".", "entries": []}
    s.tool_steps["explore"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_list", args={"path": root} if root else {}),
        ToolLoopStep(action="finish", result={"summary": "仓库是空的"}),
    )
    one_round(harness, goal("explore", "tool_loop", tools=["repo_list"], max_iterations=3), deliver(after=["explore"]))
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    assert results(harness, inp.task_id)["explore"].signals == ["environment"]


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_an_empty_subdirectory_is_not_the_environment_signal(harness: StageHarness) -> None:
    s = harness.script
    s.tools["repo_list"] = "read"
    s.tool_results["repo_list"] = lambda i: {"path": "docs", "entries": []}
    s.tool_steps["explore"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_list", args={"path": "docs"}),
        ToolLoopStep(action="finish", result={"summary": "docs 是空的"}),
    )
    one_round(harness, goal("explore", "tool_loop", tools=["repo_list"], max_iterations=3), deliver(after=["explore"]))
    inp, _ = await harness.run()
    assert results(harness, inp.task_id)["explore"].signals == []


# --- Reviewer：environment 只允许交付、问人或收尾 ---

EXPLORE = r_stage(r_goal("explore"), r_atomic("answer", role="deliver", after=("explore",)), goal_text="讲清仓库结构", exit_when="给出结构")


def empty_results(*, delivered: bool) -> dict[str, Any]:
    explore = {**ok("fetch", {"summary": "仓库是空的"}), "signals": ["environment"]}
    out: dict[str, Any] = {"explore": explore}
    out["answer"] = ok("deliver", {"text": "工作区是空的"}) if delivered else {"role": "deliver", "status": "skipped", "result": None, "cost_usd": 0.0}
    return out


def test_the_environment_signal_turns_a_replan_into_asking_a_human() -> None:
    d, fl, _ = decide(EXPLORE, empty_results(delivered=False), replan())
    assert "environment" in fl.signals and fl.allowed is not None and {"continue", "repair", "replan"}.isdisjoint(fl.allowed)
    assert d.verdict["verdict"] == "escalate" and d.verdict["escalate"]["target"] == "human"
    assert d.verdict["reason_code"] == "workspace_empty"
    assert "environment" in d.verdict["signals"]


def test_with_the_environment_signal_a_delivered_answer_can_still_be_done() -> None:
    d, _, _ = decide(EXPLORE, empty_results(delivered=True), r_done("answer"))
    assert d.verdict["verdict"] == "done", "交付“工作区是空的”这样的答复是可以的"
