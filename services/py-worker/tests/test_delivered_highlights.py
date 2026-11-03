"""F3（docs/plan/n18-file-delivery-f3.md 第 2 节第 3 条）：各轮的重要输出里，交付给用户的文件排在最前面、标出来。

- 这一轮成功调用了 deliver_file 的文件，在 highlights.files 里排第一，带 delivered: true（客户端据此给下载入口）；
  失败的调用不算。交付的文件也算“改动”，但不再重复列一次；模型给的理由留着。
- 每轮最多 5 个文件不变：交付的先占位置。
- 契约：task_state.schema.json 的 round_highlights.files 每项加可选的 delivered（布尔）——CONTRACT DRAFT，要 S-01 人审。
实现之前（round_highlights 还没有 delivered 参数）这些测试跳过。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime_worker.activities.types import ReviewRoundOutput, ToolLoopStep
from agentruntime_worker.workflows.highlights import round_highlights

from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_parallel_tool_calls import edit, read, respond, script_loop
from .test_task_verdicts import out, scripted, task_state
from .test_tool_loop_approval import IRREVERSIBLE

if "delivered" not in inspect.signature(round_highlights).parameters:
    pytest.skip("交付文件的 highlights 还没写：先审验收测试（docs/plan/n18-file-delivery-f3.md 第 2 节第 3 条）", allow_module_level=True)

STATE = {"task_id": "tsk_01JA8Q3V5K7M9P2R4T6W8Y0Z1B", "session_id": "ses_01JA8Q2N4H6K8M0P2R4T6V8X0Z", "goal": "g", "done_when": "d"}


def build(raw: Any, **kw: Any) -> dict[str, Any]:
    return round_highlights(raw, **kw)


def test_delivered_files_come_first_and_are_marked() -> None:
    raw = {"files": [{"path": "a.py", "why": "错误在这里"}]}
    files = build(raw, reads=("a.py",), writes=("b.py", "out/report.csv"), delivered=("out/report.csv",))["files"]
    assert files == [{"path": "out/report.csv", "delivered": True}, {"path": "a.py", "why": "错误在这里"}, {"path": "b.py"}]


def test_a_delivered_file_the_model_named_keeps_its_reason() -> None:
    raw = {"files": [{"path": "out/report.csv", "why": "季度报告"}]}
    files = build(raw, writes=("out/report.csv",), delivered=("./out/report.csv",))["files"]
    assert files == [{"path": "out/report.csv", "delivered": True, "why": "季度报告"}], "路径规范化后合并，不重复"


def test_delivered_files_take_the_first_of_five_places() -> None:
    delivered = tuple(f"out/{i}.csv" for i in range(7))
    files = build({"files": [{"path": "a.py", "why": "x"}]}, reads=("a.py",), writes=delivered, delivered=delivered)["files"]
    assert [f["path"] for f in files] == [f"out/{i}.csv" for i in range(5)] and all(f["delivered"] for f in files)


def test_the_marked_highlights_fit_the_task_state_schema() -> None:
    got = build(None, writes=("out/report.csv",), delivered=("out/report.csv",))
    assert_valid("task_state", {**STATE, "rounds": [{"seq": 1, "stage_goal": "s", "highlights": got}]})


temporal = pytest.mark.temporal
session_loop = pytest.mark.asyncio(loop_scope="session")


@temporal
@session_loop
async def test_a_successful_deliver_file_call_is_marked_in_task_state(harness: StageHarness) -> None:
    s = harness.script
    s.tools["deliver_file"] = "write_reversible"
    s.tool_results["deliver_file"] = lambda i: {"artifact_id": "art_01J00000000000000000000000", "path": i.args["path"], "name": "report.csv"}
    script_loop(
        harness,
        respond(read("a.py"), edit("b.py"), {"tool": "deliver_file", "args": {"path": "out/report.csv"}}),
        ToolLoopStep(action="finish", result={"done": True}),
        tools=("repo_read", "repo_edit", "deliver_file"),
    )
    s.reviewer = scripted(ReviewRoundOutput(verdict=out("done", "完成", done={"deliver_node": "answer"}).verdict, highlights={"files": [{"path": "a.py", "why": "错误在这里"}]}))
    inp, result = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    state = await task_state(harness, inp.task_id)
    assert_valid("task_state", state)
    assert state["rounds"][0]["highlights"]["files"] == [
        {"path": "out/report.csv", "delivered": True}, {"path": "a.py", "why": "错误在这里"}, {"path": "b.py"},
    ]


@temporal
@session_loop
async def test_a_failed_deliver_file_call_is_not_marked(harness: StageHarness) -> None:
    s = harness.script
    s.tools["deliver_file"] = "write_reversible"

    s.tool_results["deliver_file"] = lambda _: ValueError("delivery_file_too_large")  # 桩：返回异常就是工具报错
    script_loop(
        harness,
        respond({"tool": "deliver_file", "args": {"path": "out/huge.bin"}}),
        ToolLoopStep(action="finish", result={"done": True}),
        tools=("repo_read", "repo_edit", "deliver_file"),
    )
    s.reviewer = scripted(ReviewRoundOutput(verdict=out("done", "完成", done={"deliver_node": "answer"}).verdict))
    inp, _ = await harness.run(effect_ceiling=IRREVERSIBLE)
    state = await task_state(harness, inp.task_id)
    assert all(not f.get("delivered") for f in state["rounds"][0].get("highlights", {}).get("files", [])), "这一轮什么也没有时不写 highlights"
