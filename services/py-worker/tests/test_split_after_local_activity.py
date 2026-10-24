"""S-04 缺陷修复（D4）：local activity 之后的工作流任务切分不影响新历史的回放（见 workflows/patches.py）。

真实情况：Hook 的 local activity 运行时来了查询（客户端轮询 QueryTask），SDK 只带着 local activity 的 marker 先提交
工作流任务，之后的命令记在下一个工作流任务里；回放时结果却在上一个工作流任务里就交给了工作流，紧接着第一次问的
版本分支看不到下一个工作流任务里的标记，走了旧路径。history_split 在正常的历史上造出同样的切分：

- 修复之前录下的历史（golden events_progress，s05-human-channel 在 Hook 之后第一次问）切开后回放失败，报的就是
  夜间任务里的那个错；切开之后若紧跟的是普通命令（调度 activity），回放照常。修复之前在端到端测试里真实录下的切分历史
  回放同样失败，切开处的形状与造出来的一致：已经录下的这种历史修不了（D6）。
- 新的运行在每个 local activity 之后都切开，仍能回放：human 节点、不可逆写的审批、tool_loop 里不可逆工具调用的审批、
  Task 级挂载点的 Hook 之后推进度事件。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from temporalio.client import WorkflowHandle, WorkflowHistory
from temporalio.workflow import NondeterminismError

from agentruntime.common.v1 import enums_pb2
from agentruntime.workflow.v1 import task_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import StagePolicy, ToolLoopStep
from agentruntime_worker.replay import replay_histories
from agentruntime_worker.workflows.patches import HUMAN_CHANNEL_PATCH

from .history_split import WFT_SCHEDULED, is_local_activity_marker, patch_id, split_after, split_points, window
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .support import HISTORIES_DIR
from .test_stage_runner import atomic, deliver, goal, one_round, scripted_steps

RECORDED_SPLIT = Path(__file__).parent / "split_histories" / "human_gate_recorded_before_fix.json"
IRREVERSIBLE = enums_pb2.EFFECT_WRITE_IRREVERSIBLE


def golden(name: str) -> WorkflowHistory:
    return WorkflowHistory.from_json(name, (HISTORIES_DIR / f"{name}.json").read_text("utf-8"))


async def replay_failure(history: WorkflowHistory) -> BaseException | None:
    (result,) = await replay_histories([history])
    return result.replay_failure


def assert_patch_marker_mismatch(failure: BaseException | None, patch: str) -> None:
    assert isinstance(failure, NondeterminismError), f"应当回放失败（不确定性错误），实际 {failure!r}"
    assert "patch marker" in str(failure) and patch in str(failure), f"应当是版本分支 {patch} 的标记对不上：{failure}"


# --- 修复之前录下的历史 ---


@pytest.mark.asyncio(loop_scope="session")
async def test_old_history_split_after_the_hook_before_a_question_does_not_replay() -> None:
    history = golden("events_progress")
    events = list(history.events)
    assert await replay_failure(history) is None, "不切开时 golden 能回放"
    (before_ask,) = split_points(events, before_activity=names.ASK_HUMAN)
    assert_patch_marker_mismatch(await replay_failure(split_after(history, [before_ask])), HUMAN_CHANNEL_PATCH)
    # 切开处之后紧跟的是普通命令（调度 activity）时，回放照常：出问题的只是 local activity 之后第一次问的版本分支。
    plain = [p for p in split_points(events) if p != before_ask]
    assert plain, "golden 里还有别的 Hook 之后紧跟着调度 activity"
    assert await replay_failure(split_after(history, plain)) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_split_recorded_before_the_fix_cannot_be_replayed_and_matches_the_synthetic_shape() -> None:
    recorded = WorkflowHistory.from_json("recorded", RECORDED_SPLIT.read_text("utf-8"))
    assert_patch_marker_mismatch(await replay_failure(recorded), HUMAN_CHANNEL_PATCH)
    events = list(recorded.events)
    # 真实录下的切分：local activity 的 marker 之后紧跟着一个新的工作流任务，s05-human-channel 记在那个工作流任务里。
    real = next(
        i
        for i in range(len(events) - 4)
        if is_local_activity_marker(events[i]) and events[i + 1].event_type == WFT_SCHEDULED and patch_id(events[i + 4]) == HUMAN_CHANNEL_PATCH
    )
    history = golden("events_progress")
    (before_ask,) = split_points(list(history.events), before_activity=names.ASK_HUMAN)
    assert window(events, real) == window(list(split_after(history, [before_ask]).events), before_ask), "造出来的切分与真实录下的形状一致"


# --- 新的运行 ---


def reply(question_id: str) -> task_pb2.HumanReplySignal:
    return task_pb2.HumanReplySignal(question_id=question_id, option="approve", replied_by_user_id="u1")


async def answered(h: StageHarness, task_id: str, handle: WorkflowHandle, question_id: str) -> WorkflowHistory:  # type: ignore[type-arg]
    await h.wait_for(lambda: any(q.task_id == task_id and q.question_id == question_id for q in h.script.questions), f"问题 {question_id}")
    await handle.signal(names.HUMAN_REPLY_SIGNAL, reply(question_id))
    result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    return await handle.fetch_history()


async def assert_replays_when_split_after_every_local_activity(history: WorkflowHistory, *, before_activity: str) -> None:
    events = list(history.events)
    assert split_points(events, before_activity=before_activity), f"{before_activity} 之前应当紧挨着一个 Hook（local activity），能切开"
    points = split_points(events)
    split = split_after(history, points)
    assert len(list(split.events)) == len(events) + 3 * len(points)
    failure = await replay_failure(split)
    assert failure is None, f"在每个 local activity 之后切开（{len(points)} 处）应当仍能回放：{failure}"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_new_human_node_history_replays_when_split_after_hooks(harness: StageHarness) -> None:
    confirm = atomic("confirm", executor="human", role="gate", template="confirm_scope@v1", inputs={"question": "按华东的范围继续吗？", "options": ["approve", "reject"]})
    one_round(harness, confirm, deliver(after=["confirm"]))
    inp, handle = await harness.start()
    history = await answered(harness, inp.task_id, handle, "confirm")
    await assert_replays_when_split_after_every_local_activity(history, before_activity=names.ASK_HUMAN)


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_new_approval_history_replays_when_split_after_hooks(harness: StageHarness) -> None:
    one_round(harness, atomic("pay_ticket", effect="write_irreversible"), deliver(after=["pay_ticket"]))
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    history = await answered(harness, inp.task_id, handle, "pay_ticket.1")
    await assert_replays_when_split_after_every_local_activity(history, before_activity=names.ASK_HUMAN)


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_new_tool_loop_approval_history_replays_when_split_after_hooks(harness: StageHarness) -> None:
    s = harness.script
    s.tools.update({"card_charge": "write_irreversible"})
    s.tool_steps["book"] = scripted_steps(ToolLoopStep(action="tool", tool="card_charge", args={"flight": "MU5101"}), ToolLoopStep(action="finish", result={"ticket": "781"}))
    one_round(harness, goal("book", "tool_loop", tools=["card_charge"], effect_ceiling="write_irreversible"), deliver(after=["book"]))
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    history = await answered(harness, inp.task_id, handle, "book.2.1")
    # tool_loop 里的审批前面是 tool_loop_step（普通 activity）；Hook 在 Goal Node 之前，之后紧跟着第一步 tool_loop_step。
    await assert_replays_when_split_after_every_local_activity(history, before_activity=names.TOOL_LOOP_STEP)


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_new_history_with_task_level_hooks_replays_when_split_after_them(harness: StageHarness) -> None:
    s = harness.script
    s.policy = StagePolicy(hooks={"on_task_received": ["content_filter"], "on_task_start": ["project_rules"]})
    one_round(harness, deliver())
    inp, result = await harness.run()
    assert result.status == enums_pb2.TASK_STATUS_DONE
    history = await harness.client.get_workflow_handle(inp.task_id).fetch_history()
    # on_task_start 的 Hook 之后是 Round 循环：修复之前 t11b-progress-events 在这里第一次问，紧跟着推 round_started。
    await assert_replays_when_split_after_every_local_activity(history, before_activity=names.PUBLISH_TASK_EVENT)
