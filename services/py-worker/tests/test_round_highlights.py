"""docs/plan/running-task-interaction.md 第 4 节验收：各轮的重要输出（重要文件、重要答案），给人看的汇总。

- 走 Reviewer 模型的轮：Progress Check 的回答多一个 highlights，同一次调用，不多花一次模型请求。
- fast 的轮，或者 Reviewer 模型没给出可用的 highlights：代码取回答开头两句，不调模型。
- 文件路径由代码核对：模型写的路径要是这一轮工具调用的目标，否则去掉（防编造）。改动过的文件由代码补上。
- 每轮最多 5 个文件、5 条答案，每条答案不超过 300 字。写进 Task State 的 rounds[]，不给 Planner 与 Reviewer。

相对计划的一处收窄：计划写的是“工具调用目标或工作区里存在”。工作流不能做 I/O，Reviewer 碰不到工作区，所以只核对工具调用目标：
这一轮没碰过的文件，也算不上这一轮的重要输出。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime.common.v1 import enums_pb2
from agentruntime_worker import names
from agentruntime_worker.activities.types import ReviewRoundOutput, ToolLoopStep
from agentruntime_worker.reviewer import prompts
from agentruntime_worker.reviewer.reviewer import Reviewer

from .reviewer_support import FakeReviewGateway, answer, ok, request, rounds
from .reviewer_support import atomic as review_atomic
from .reviewer_support import stage as review_stage
from .reviewer_support import task_state as review_state
from .stage_harness import StageHarness, harness, returns  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_parallel_tool_calls import edit, read, respond, script_loop
from .test_stage_runner import deliver, goal, one_round
from .test_task_guidance import entered, signal, wait_for_state, two_rounds
from .test_task_verdicts import out, scripted, task_state
from .test_tool_loop_approval import IRREVERSIBLE

HIGHLIGHTS = {"files": [{"path": "a.py", "why": "错误在这里"}], "answers": ["失败是因为取整用了 int()。"]}


def a_request() -> Any:
    return request(review_stage(review_atomic("fix", role="act", effect="write_reversible")), {"fix": ok("act")})


def build(raw: Any, *, reads: tuple[str, ...] = (), writes: tuple[str, ...] = (), deliverable: Any = None) -> dict[str, Any]:
    from agentruntime_worker.workflows.highlights import round_highlights

    return round_highlights(raw, reads=reads, writes=writes, deliverable=deliverable)


# --- Reviewer：同一次调用 ---


def test_the_progress_check_asks_for_highlights() -> None:
    assert "highlights" in prompts.REVIEW_OUTPUT_SCHEMA["properties"]
    assert "highlights" in prompts._REVIEWER_RULES


async def test_highlights_come_from_the_same_call() -> None:
    gateway = FakeReviewGateway([answer({"verdict": "continue"}, highlights=HIGHLIGHTS)])
    result = await Reviewer(gateway).review(a_request())
    assert len(gateway.requests) == 1, "不多花一次模型请求"
    assert result.highlights == HIGHLIGHTS


@pytest.mark.parametrize("bad", ["不是对象", {"files": "不是列表"}, {"answers": [1, 2]}, {"files": [{"why": "没有路径"}]}, 3])
async def test_a_bad_highlights_is_dropped_without_asking_again(bad: Any) -> None:
    """highlights 是给人看的，判定才要紧：不合格的丢掉，不为它重问（重问要花钱）。"""
    gateway = FakeReviewGateway([answer({"verdict": "continue"}, highlights=bad)])
    result = await Reviewer(gateway).review(a_request())
    assert len(gateway.requests) == 1
    assert result.verdict["verdict"] == "continue"
    assert not result.highlights


async def test_a_fast_round_makes_no_model_call() -> None:
    gateway = FakeReviewGateway([])
    fast = request(review_stage(review_atomic("answer", role="deliver")), {"answer": ok("deliver", {"answer": "你好。"})}, state=review_state(rounds=rounds(triage="fast")))
    result = await Reviewer(gateway).review(fast)
    assert gateway.requests == []
    assert not result.highlights


def test_reviewers_view_has_no_highlights() -> None:
    state = {"task_id": "t", "goal": "g", "done_when": "d", "rounds": [{"seq": 1, "stage_goal": "s", "highlights": HIGHLIGHTS}]}
    view = prompts.review_view(state, {"seq": 2, "goal": "g", "nodes": []}, {}, [])
    assert "highlights" not in view["task_state"]["rounds"][0]


# --- 代码核对与补上 ---


def test_a_path_the_round_never_touched_is_removed() -> None:
    raw = {"files": [{"path": "a.py", "why": "读过"}, {"path": "made_up.py", "why": "编造的"}]}
    assert [f["path"] for f in build(raw, reads=("a.py",))["files"]] == ["a.py"]


def test_changed_files_are_added_by_code() -> None:
    raw = {"files": [{"path": "a.py", "why": "错误在这里"}]}
    files = build(raw, reads=("a.py",), writes=("b.py",))["files"]
    assert files == [{"path": "a.py", "why": "错误在这里"}, {"path": "b.py"}]


def test_a_changed_file_the_model_named_keeps_its_reason() -> None:
    files = build({"files": [{"path": "b.py", "why": "改了这里"}]}, writes=("b.py",))["files"]
    assert files == [{"path": "b.py", "why": "改了这里"}], "不重复"


def test_paths_are_compared_after_normalizing() -> None:
    assert [f["path"] for f in build({"files": [{"path": "./src/a.py", "why": "x"}]}, reads=("src/a.py",))["files"]] == ["src/a.py"]


def test_at_most_five_of_each() -> None:
    raw = {"files": [{"path": f"{i}.py", "why": "x"} for i in range(7)], "answers": [f"第 {i} 条。" for i in range(7)]}
    got = build(raw, reads=tuple(f"{i}.py" for i in range(7)))
    assert len(got["files"]) == 5 and len(got["answers"]) == 5
    assert_valid("task_state", {"task_id": "tsk_01JA8Q3V5K7M9P2R4T6W8Y0Z1B", "session_id": "ses_01JA8Q2N4H6K8M0P2R4T6V8X0Z", "goal": "g", "done_when": "d", "rounds": [{"seq": 1, "stage_goal": "s", "highlights": got}]})


def test_an_answer_is_cut_to_300_characters() -> None:
    [text] = build({"answers": ["长" * 500]})["answers"]
    assert len(text) <= 300 and text.endswith("…")


def test_why_is_cut_too() -> None:
    [f] = build({"files": [{"path": "a.py", "why": "长" * 500}]}, reads=("a.py",))["files"]
    assert len(f["why"]) <= 300


def test_credentials_are_redacted() -> None:
    """模型可能把工具输出里的令牌抄进来；Task State 给客户端看。"""
    got = build({"answers": ["令牌是 ghp_0123456789abcdefghijklmnopqrstuvwxyz0"], "files": [{"path": "a.py", "why": "password=hunter2hunter2"}]}, reads=("a.py",))
    assert "ghp_0123456789abcdefghijklmnopqrstuvwxyz0" not in got["answers"][0]
    assert "hunter2hunter2" not in got["files"][0]["why"]


# --- 没有模型给的答案：代码取开头两句 ---


@pytest.mark.parametrize(
    ("deliverable", "want"),
    [
        ("第一句。第二句。第三句。", "第一句。第二句。"),
        ("One sentence. Two sentences! Three?", "One sentence. Two sentences!"),
        ("只有一句", "只有一句"),
        ({"answer": "第一句。第二句。第三句。"}, "第一句。第二句。"),
        ({"text": "第一句！第二句？第三句。"}, "第一句！第二句？"),
    ],
)
def test_without_answers_it_takes_the_first_two_sentences(deliverable: Any, want: str) -> None:
    assert build(None, deliverable=deliverable)["answers"] == [want]


def test_a_long_first_sentence_is_cut() -> None:
    [text] = build(None, deliverable="长" * 500)["answers"]
    assert len(text) <= 300


def test_a_version_number_does_not_end_the_sentence() -> None:
    assert build(None, deliverable="升级到 1.2.3 就好了。其次是配置。第三。")["answers"] == ["升级到 1.2.3 就好了。其次是配置。"]


@pytest.mark.parametrize("deliverable", [None, "", {"rows": [1, 2]}, 3, []])
def test_nothing_to_quote_means_no_answers(deliverable: Any) -> None:
    assert build(None, deliverable=deliverable).get("answers", []) == []


def test_model_answers_win_over_the_deliverable() -> None:
    assert build({"answers": ["模型挑的。"]}, deliverable="全文开头。")["answers"] == ["模型挑的。"]


def test_an_empty_round_has_empty_highlights() -> None:
    assert build(None) == {}


# --- 工作流：写进 Task State ---

temporal = pytest.mark.temporal
session_loop = pytest.mark.asyncio(loop_scope="session")


@temporal
@session_loop
async def test_a_rounds_highlights_are_in_task_state(harness: StageHarness) -> None:
    s = harness.script
    script_loop(harness, respond(read("a.py"), edit("b.py")), ToolLoopStep(action="finish", result={"done": True}))
    highlights = {"files": [{"path": "a.py", "why": "错误在这里"}, {"path": "made_up.py", "why": "编造的"}], "answers": ["取整用错了。"]}
    s.reviewer = scripted(ReviewRoundOutput(verdict=out("done", "完成", done={"deliver_node": "answer"}).verdict, highlights=highlights))
    inp, result = await harness.run(effect_ceiling=IRREVERSIBLE)
    assert result.status == enums_pb2.TASK_STATUS_DONE
    state = await task_state(harness, inp.task_id)
    assert_valid("task_state", state)
    assert state["rounds"][0]["highlights"] == {"files": [{"path": "a.py", "why": "错误在这里"}, {"path": "b.py"}], "answers": ["取整用错了。"]}


@temporal
@session_loop
async def test_a_fast_round_quotes_the_deliverable_without_a_model(harness: StageHarness) -> None:
    s = harness.script
    s.triage_level = "fast"
    s.behaviors["answer"] = returns({"answer": "你好。我是助手。有什么要帮忙的？"})
    one_round(harness, deliver())
    inp, _ = await harness.run()
    state = await task_state(harness, inp.task_id)
    assert state["rounds"][0]["highlights"]["answers"] == ["你好。我是助手。"]


@temporal
@session_loop
async def test_a_round_replanned_by_guidance_still_records_its_changed_files(harness: StageHarness) -> None:
    """用户纠偏的轮没有 Reviewer 的回答，改过的文件还是要列出来。"""
    s = harness.script
    hold = s.hold(f"{names.CALL_TOOL}:fix.2")
    s.tools.update({"repo_read": "read", "repo_edit": "write_reversible"})
    s.tool_results["repo_edit"] = lambda i: {"edited": i.args["path"]}
    s.tool_steps["fix"] = lambda inp: respond(edit("b.py")) if inp.iteration == 1 else ToolLoopStep(action="finish", result={"done": True})
    two_rounds(harness, goal("fix", "tool_loop", tools=["repo_edit"], effect_ceiling="write_reversible"), deliver(after=["fix"]))
    inp, handle = await harness.start(effect_ceiling=IRREVERSIBLE)
    await entered(harness, hold)
    await handle.signal(names.TASK_GUIDANCE_SIGNAL, signal("g1", "再看看"))
    await wait_for_state(harness, inp.task_id, "g1", "pending")
    hold.release.set()
    await handle.result()
    state = await task_state(harness, inp.task_id)
    assert {"path": "b.py"} in state["rounds"][0]["highlights"]["files"]

