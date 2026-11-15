"""TMP 计划第 11.15 节，调优第一轮：判“完成”要有证据。

基线里判 done 的 46 题有 15 题其实没解决。但不能写成“测试通过才算完成”：最后一次测试非零退出的 20 题里 12 题其实解决了（项目本来就有失败的测试）。
所以只拦两种明显没做完的情况，而且各只拦一次（不会卡死），其余把事实交给 Reviewer 判断：

- 能改文件却一个文件都没改，就判 done（下午那次 django-11433）。
- 改完之后没有运行过任何命令（基线 6 题，2 题没解决）。
- 最后一次命令的退出码告诉 Reviewer。

记录由 Stage Runner 给（Node 的 result 是模型写的，不算证据），只加活动的输入，不改命令序列。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime.common.v1 import enums_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import ToolLoopStep
from agentruntime_worker.reviewer import prompts
from agentruntime_worker.reviewer.rules import Caps, Decision, Thresholds, enforce, floor
from agentruntime_worker.reviewer.signals import ledger

from .reviewer_support import CATALOG, atomic, cont, done, goal, ok, rounds, stage, task_state
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_stage_runner import deliver as stage_deliver
from .test_stage_runner import goal as stage_goal
from .test_stage_runner import one_round, scripted_steps

FIX_STAGE = stage(goal("fix"), atomic("report", role="deliver", template="answer@v1", executor="model"))
RESULTS = {"fix": ok(), "report": ok("deliver", {"text": "修好了"})}


def verified(**over: Any) -> dict[str, Any]:
    return {"write_capable": True, "wrote": True, "ran_after_write": True, "last_exit_code": 0, "done_blocked": 0, **over}


def decide(proposal: dict[str, Any] | None, verification: dict[str, Any] | None) -> Decision:
    state = task_state(rounds=rounds())
    lg = ledger(state, FIX_STAGE, RESULTS, [])
    fl = floor(lg, RESULTS, completion=1.0, thresholds=Thresholds())
    decision = enforce(None if proposal is None else proposal["verdict"], floor=fl, ledger=lg, stage=FIX_STAGE, results=RESULTS, catalog=CATALOG, caps=Caps(), verification=verification)
    assert_valid("verdict", decision.verdict)
    return decision


# --- 明显没做完的，不算完成 ---


def test_done_after_changes_and_a_command_stands() -> None:
    assert decide(done("report"), verified()).verdict["verdict"] == "done"


def test_done_without_running_anything_after_the_change_is_not_done() -> None:
    d = decide(done("report"), verified(ran_after_write=False, last_exit_code=None))
    assert d.verdict["verdict"] == "continue"
    assert d.verdict["reason_code"] == "rule_done_unverified"
    assert d.raised


def test_done_without_any_change_is_done_once_the_model_accepted_it() -> None:
    # 有写权限不等于要写（reviewer/rules.py）：没有改动、模型已按用户要求验收的回答直接完成，不再追问改动。
    d = decide(done("report"), verified(wrote=False, ran_after_write=False, last_exit_code=None))
    assert d.verdict["verdict"] == "done"


def test_a_task_that_cannot_change_files_may_finish_without_changes() -> None:
    """调研、问答没有写工具：什么都没改是对的。"""
    d = decide(done("report"), verified(write_capable=False, wrote=False, ran_after_write=False, last_exit_code=None))
    assert d.verdict["verdict"] == "done"


@pytest.mark.parametrize(
    "facts",
    [{"ran_after_write": False, "last_exit_code": None}, {"wrote": False, "ran_after_write": False, "last_exit_code": None}],
    ids=["unverified", "no_changes"],
)
def test_it_only_stops_done_once(facts: dict[str, Any]) -> None:
    """有的任务确实不用改、改完也没法跑（比如只改文档）：拦过一次，Reviewer 还是判 done 就算数，不能卡死。"""
    d = decide(done("report"), verified(**facts, done_blocked=1))
    assert d.verdict["verdict"] == "done"


def test_a_failing_last_command_does_not_block_done() -> None:
    """基线里最后一次非零退出的 20 题有 12 题解决了：项目本来就可能有失败的测试。这个判断交给 Reviewer。"""
    assert decide(done("report"), verified(last_exit_code=1)).verdict["verdict"] == "done"


def test_the_rules_only_path_checks_the_same_thing() -> None:
    """模型不可用时只按规则判定：各 Node 成功、deliver 给了结果，原来就判 done。"""
    d = decide(None, verified(ran_after_write=False, last_exit_code=None))
    assert d.verdict["verdict"] == "continue" and d.verdict["reason_code"] == "rule_done_unverified"


def test_continue_is_left_alone() -> None:
    assert decide(cont(), verified(ran_after_write=False, last_exit_code=None)).verdict["verdict"] == "continue"


def test_callers_without_the_record_behave_as_before() -> None:
    """评测样本与老的运行没有这份记录。"""
    assert decide(done("report"), None).verdict["verdict"] == "done"


# --- 事实告诉 Reviewer ---


def test_the_reviewer_is_told_the_facts() -> None:
    state = task_state(rounds=rounds())
    lg = ledger(state, FIX_STAGE, RESULTS, [])
    fl = floor(lg, RESULTS, completion=None, thresholds=Thresholds())
    view = prompts.review_view(state, FIX_STAGE, RESULTS, [])
    text = "\n".join(m.text for m in prompts.review_messages(view, ledger=lg, floor=fl, catalog=CATALOG, verification=verified(last_exit_code=1)))
    assert '"last_exit_code":1' in text and '"ran_after_write":true' in text
    assert "已有的失败" in text, "提示 Reviewer 非零退出要分清是这次改坏的还是本来就有的"


# --- 记录来自 Stage Runner 真正执行的调用 ---


def tools(harness_: StageHarness, **results: Any) -> None:
    harness_.script.tools.update({"repo_edit": "write_reversible", "shell_run": "write_reversible", "repo_read": "read"})
    harness_.script.tool_results.update(results)


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_change_then_a_command_is_recorded(harness: StageHarness) -> None:  # noqa: F811
    s = harness.script
    tools(harness, shell_run=lambda i: {"exit_code": 1, "stdout": "", "stderr": ""})
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_edit", args={"path": "a.py", "new": "x"}),
        ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["python", "-m", "pytest"]}),
        ToolLoopStep(action="finish", result={"ok": True}),
    )
    one_round(harness, stage_goal("fix", "tool_loop", tools=["repo_edit", "shell_run"]), stage_deliver(after=["fix"]))
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert s.review(inp.task_id, 1).verification == {"write_capable": True, "wrote": True, "ran_after_write": True, "last_exit_code": 1, "done_blocked": 0}


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_command_before_the_change_does_not_count(harness: StageHarness) -> None:  # noqa: F811
    s = harness.script
    tools(harness, shell_run=lambda i: {"exit_code": 0, "stdout": "", "stderr": ""})
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["git", "status"]}),
        ToolLoopStep(action="tool", tool="repo_edit", args={"path": "a.py", "new": "x"}),
        ToolLoopStep(action="finish", result={"ok": True}),
    )
    one_round(harness, stage_goal("fix", "tool_loop", tools=["repo_edit", "shell_run"]), stage_deliver(after=["fix"]))
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    record = s.review(inp.task_id, 1).verification
    assert record["wrote"] is True and record["ran_after_write"] is False and record["last_exit_code"] is None


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_task_that_only_read_is_recorded(harness: StageHarness) -> None:  # noqa: F811
    s = harness.script
    tools(harness)
    s.tool_steps["fix"] = scripted_steps(ToolLoopStep(action="tool", tool="repo_read", args={"path": "a.py"}), ToolLoopStep(action="finish", result={"ok": True}))
    one_round(harness, stage_goal("fix", "tool_loop", tools=["repo_read", "repo_edit"]), stage_deliver(after=["fix"]))
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    record = s.review(inp.task_id, 1).verification
    assert record["write_capable"] is True and record["wrote"] is False


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_node_without_write_tools_is_not_write_capable(harness: StageHarness) -> None:  # noqa: F811
    s = harness.script
    tools(harness)
    s.tool_steps["fix"] = scripted_steps(ToolLoopStep(action="tool", tool="repo_read", args={"path": "a.py"}), ToolLoopStep(action="finish", result={"ok": True}))
    one_round(harness, stage_goal("fix", "tool_loop", tools=["repo_read"]), stage_deliver(after=["fix"]))
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert s.review(inp.task_id, 1).verification["write_capable"] is False


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_edit_is_not_a_change(harness: StageHarness) -> None:  # noqa: F811
    s = harness.script
    tools(harness, repo_edit=lambda i: {"error": "old 在文件里没有出现"})
    s.tool_steps["fix"] = scripted_steps(ToolLoopStep(action="tool", tool="repo_edit", args={"path": "a.py", "old": "y", "new": "x"}), ToolLoopStep(action="finish", result={"ok": True}))
    one_round(harness, stage_goal("fix", "tool_loop", tools=["repo_edit"]), stage_deliver(after=["fix"]))
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert s.review(inp.task_id, 1).verification["wrote"] is False


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_the_record_carries_across_rounds(harness: StageHarness) -> None:  # noqa: F811
    """第一轮改、第二轮跑：是同一件事。"""
    s = harness.script
    tools(harness, shell_run=lambda i: {"exit_code": 0, "stdout": "", "stderr": ""})
    s.tool_steps["edit"] = scripted_steps(ToolLoopStep(action="tool", tool="repo_edit", args={"path": "a.py", "new": "x"}), ToolLoopStep(action="finish", result={"ok": True}))
    s.tool_steps["check"] = scripted_steps(ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["python", "-m", "pytest"]}), ToolLoopStep(action="finish", result={"ok": True}))
    s.plans["main"] = [
        {"seq": 1, "goal": "改", "exit_when": "完成", "nodes": [stage_goal("edit", "tool_loop", tools=["repo_edit"])]},
        {"seq": 2, "goal": "跑", "exit_when": "完成", "nodes": [stage_goal("check", "tool_loop", tools=["shell_run"]), stage_deliver(after=["check"])]},
    ]
    s.deliver["main"] = "answer"
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert s.review(inp.task_id, 1).verification["ran_after_write"] is False
    assert s.review(inp.task_id, 2).verification["ran_after_write"] is True
    calls = [c for c in s.calls if c.activity == names.TOOL_LOOP_STEP and c.workflow_id == inp.task_id]
    assert calls, "两轮都走了 tool_loop"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_reading_after_the_command_does_not_replace_its_exit_code(harness: StageHarness) -> None:  # noqa: F811
    """测试失败之后 git diff 一下，退出码不能变成 0；Reviewer 要看到的是那次测试。"""
    s = harness.script
    tools(harness, shell_run=lambda i: {"exit_code": 0 if i.args["argv"][0] == "git" else 1, "stdout": "", "stderr": ""})
    s.tool_steps["fix"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_edit", args={"path": "a.py", "new": "x"}),
        ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["python", "-m", "pytest"]}),
        ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["git", "diff"]}),
        ToolLoopStep(action="finish", result={"ok": True}),
    )
    one_round(harness, stage_goal("fix", "tool_loop", tools=["repo_edit", "shell_run"]), stage_deliver(after=["fix"]))
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert s.review(inp.task_id, 1).verification["last_exit_code"] == 1


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_only_a_shell_does_not_make_a_node_write_capable(harness: StageHarness) -> None:  # noqa: F811
    """很多只读的任务靠 shell_run 查东西：什么都没改是对的，不该为此多跑一轮。"""
    s = harness.script
    tools(harness, shell_run=lambda i: {"exit_code": 0, "stdout": "", "stderr": ""})
    s.tool_steps["fix"] = scripted_steps(ToolLoopStep(action="tool", tool="shell_run", args={"argv": ["git", "log"]}), ToolLoopStep(action="finish", result={"ok": True}))
    one_round(harness, stage_goal("fix", "tool_loop", tools=["shell_run"]), stage_deliver(after=["fix"]))
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert s.review(inp.task_id, 1).verification["write_capable"] is False


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_blocked_done_is_counted_once_for_the_next_review(harness: StageHarness) -> None:  # noqa: F811
    """Reviewer 的 reason_code 进记录：第二次还判 done 就算数，不能卡死。"""
    s = harness.script
    tools(harness)
    s.tool_steps["a"] = scripted_steps(ToolLoopStep(action="tool", tool="repo_read", args={"path": "a.py"}), ToolLoopStep(action="finish", result={"ok": True}))
    s.tool_steps["b"] = s.tool_steps["a"]
    s.plans["main"] = [
        {"seq": 1, "goal": "看", "exit_when": "完成", "nodes": [stage_goal("a", "tool_loop", tools=["repo_read", "repo_edit"])]},
        {"seq": 2, "goal": "再看", "exit_when": "完成", "nodes": [stage_goal("b", "tool_loop", tools=["repo_read", "repo_edit"]), stage_deliver(after=["b"])]},
    ]
    s.deliver["main"] = "answer"

    async def reviewer(inp: Any) -> dict[str, Any]:
        if inp.round_seq == 1:
            return {"verdict": "continue", "reason": "没改", "reason_code": "rule_done_without_changes"}
        return {"verdict": "done", "reason": "脚本：完成", "done": {"deliver_node": "answer"}}

    s.reviewer = reviewer
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert s.review(inp.task_id, 1).verification["done_blocked"] == 0
    assert s.review(inp.task_id, 2).verification["done_blocked"] == 1
