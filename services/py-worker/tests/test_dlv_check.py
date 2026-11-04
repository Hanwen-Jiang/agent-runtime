"""DLV-CHECK（docs/plan/all-plans-dag-2026-11-03.md 第 3 节；第 26 版冒烟发现）：交付的真假。

要求用 deliver_file 交付 result.txt，模型没调这个工具，回答里却写“交付成功（delivered: true）”，Reviewer 也认了，ListTaskArtifacts 是空的。

1. Stage Runner 真正成功的 deliver_file 调用（工具没报错）的 path 记进 Task 的 VerificationRecord，跨轮累计，随 verification 交给 Reviewer；
   失败的不算。Node 的 result 是模型写的，不算证据。deliver_file 不算改文件：交付在验证之后，不抹掉“改完运行过命令”。
2. 回答说把文件交付了（点名 deliver_file、delivered: true、“已交付 result.txt”“文件已上传”“可以下载”等），这个 Task 却一个文件也没交付：
   判 done 拦一次，改判 continue（rule_done_undelivered）；规则兜底的路径同样拦。交付过文件、回答没说交付文件、拦过一次之后都照常。
3. Reviewer 的提示里有 delivered，说明以它为准；Catalog 里有 deliver_file 时，Planner 的提示要求做文件的 Goal Node 带上它、done_when 写明交付哪些文件。
4. 只加活动的输入，不改命令序列；没有这份记录的调用方（评测样本、之前的运行）照旧。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime.common.v1 import enums_pb2

from agentruntime_worker.activities.types import ToolLoopStep
from agentruntime_worker.planner import prompts as planner_prompts
from agentruntime_worker.planner.context import CatalogView
from agentruntime_worker.reviewer import prompts
from agentruntime_worker.reviewer.rules import Caps, Decision, Thresholds, delivery_claimed, enforce, floor
from agentruntime_worker.reviewer.signals import ledger
from agentruntime_worker.workflows.verification import VerificationRecord

from .reviewer_support import CATALOG, atomic, cont, done, goal, ok, rounds, stage, task_state
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_stage_runner import deliver as stage_deliver
from .test_stage_runner import goal as stage_goal
from .test_stage_runner import one_round, scripted_steps

FILE_STAGE = stage(goal("make"), atomic("report", role="deliver", template="answer@v1", executor="model"))
CLAIM = "已用 deliver_file 交付 result.txt，交付成功（delivered: true）。"


def record(**over: Any) -> dict[str, Any]:
    """改过文件、验证过（不让 TMP 11.15 的两条规则插手），没有交付过文件。"""
    return {"write_capable": True, "wrote": True, "ran_after_write": True, "last_exit_code": 0, "done_blocked": 0, **over}


def decide(proposal: dict[str, Any] | None, reply: Any, verification: dict[str, Any] | None) -> Decision:
    results = {"make": ok(), "report": ok("deliver", {"text": reply} if isinstance(reply, str) else reply)}
    state = task_state(rounds=rounds())
    lg = ledger(state, FILE_STAGE, results, [])
    fl = floor(lg, results, completion=1.0, thresholds=Thresholds())
    decision = enforce(
        None if proposal is None else proposal["verdict"], floor=fl, ledger=lg, stage=FILE_STAGE, results=results, catalog=CATALOG, caps=Caps(), verification=verification
    )
    assert_valid("verdict", decision.verdict)
    return decision


# --- 说交付了却没交付，不算完成 ---


def test_a_claimed_delivery_without_deliver_file_is_not_done() -> None:
    d = decide(done("report"), CLAIM, record())
    assert d.verdict["verdict"] == "continue" and d.verdict["reason_code"] == "rule_done_undelivered" and d.raised
    assert "deliver_file" in d.verdict["reason"]


def test_a_real_delivery_stands() -> None:
    assert decide(done("report"), CLAIM, record(delivered=["result.txt"])).verdict["verdict"] == "done"


def test_a_reply_that_claims_no_file_is_left_alone() -> None:
    for reply in ("修好了，测试都过了。", "已提交到分支 fix/rounding，改了 calc.py。", "Fixed the rounding bug; all tests pass."):
        assert decide(done("report"), reply, record()).verdict["verdict"] == "done", reply


def test_it_only_stops_done_once() -> None:
    """拦过一次还判 done 就算数：认错了交付的说法也不能卡死。"""
    assert decide(done("report"), CLAIM, record(delivery_blocked=1)).verdict["verdict"] == "done"


def test_the_rules_only_path_checks_the_same_thing() -> None:
    d = decide(None, CLAIM, record())
    assert d.verdict["verdict"] == "continue" and d.verdict["reason_code"] == "rule_done_undelivered"


def test_continue_is_left_alone_and_old_callers_behave_as_before() -> None:
    assert decide(cont(), CLAIM, record()).verdict["verdict"] == "continue"
    assert decide(done("report"), CLAIM, None).verdict["verdict"] == "done", "评测样本与老的运行没有这份记录"


@pytest.mark.parametrize(
    "reply",
    [
        CLAIM,
        {"summary": "文件已经生成", "delivered": True},
        "result.txt 已交付，可以下载。",
        "已上传文件 report.pdf。",
        "I delivered report.csv with deliver_file.",
        "The file has been uploaded and is available for download.",
    ],
)
def test_delivery_claims_are_recognised(reply: Any) -> None:
    assert delivery_claimed(reply)


@pytest.mark.parametrize(
    "reply",
    ["修好了，测试都过了。", "已交付修复：calc.py 的四舍五入改对了。", "The fix is delivered in calc.py; tests pass.", {"ok": True}, None],
)
def test_other_replies_are_not_claims(reply: Any) -> None:
    assert not delivery_claimed(reply)


# --- 记录与提示 ---


def test_the_record_keeps_every_delivered_file_once() -> None:
    r = VerificationRecord()
    assert "delivered" not in r.as_dict() and "delivery_blocked" not in r.as_dict(), "没交付、没拦过时记录照旧"
    r.delivered_files(["./out/result.txt", "out\\result.txt"])
    r.delivered_files(["b.csv"])
    r.reviewed({"verdict": "continue", "reason_code": "rule_done_undelivered"})
    assert r.as_dict()["delivered"] == ["out/result.txt", "b.csv"] and r.as_dict()["delivery_blocked"] == 1
    assert r.as_dict()["done_blocked"] == 0, "与 TMP 11.15 的两条规则各算各的"


def test_delivering_is_not_a_change_to_the_files() -> None:
    """deliver_file 只是把文件交给用户：交付在验证之后，不能把“改完运行过命令”抹掉；只给它的节点也不算能改文件。"""
    r = VerificationRecord()
    r.called("repo_edit", "write_reversible", {"path": "a.py"}, {"ok": True})
    r.called("shell_run", "write_reversible", {"argv": ["python", "-m", "pytest"]}, {"exit_code": 0})
    r.called("deliver_file", "write_reversible", {"path": "./a.py"}, {"artifact_id": "art_1"})
    r.called("deliver_file", "write_reversible", {"path": "b.py"}, {"error": "工作区里没有文件 b.py"})
    d = r.as_dict()
    assert d["ran_after_write"] is True and d["last_exit_code"] == 0 and d["delivered"] == ["a.py"]
    only = VerificationRecord()
    only.offered({"deliver_file": "write_reversible", "repo_read": "read"})
    assert only.as_dict()["write_capable"] is False


def test_the_reviewer_is_told_what_was_delivered() -> None:
    results = {"make": ok(), "report": ok("deliver", {"text": CLAIM})}
    state = task_state(rounds=rounds())
    lg = ledger(state, FILE_STAGE, results, [])
    fl = floor(lg, results, completion=None, thresholds=Thresholds())
    view = prompts.review_view(state, FILE_STAGE, results, [])
    text = "\n".join(m.text for m in prompts.review_messages(view, ledger=lg, floor=fl, catalog=CATALOG, verification=record(delivered=["result.txt"])))
    assert '"delivered":["result.txt"]' in text
    assert "delivered" in prompts._REVIEWER_RULES and "deliver_file" in prompts._REVIEWER_RULES, "提示里说明交付以 delivered 为准"


def test_the_planner_asks_for_deliver_file_when_the_catalog_has_it() -> None:
    with_tool = planner_prompts.system_zone(CatalogView(tools={"deliver_file": "write_reversible", "repo_edit": "write_reversible"}))[0].text
    without = planner_prompts.system_zone(CatalogView(tools={"repo_edit": "write_reversible"}))[0].text
    assert "done_when" in with_tool.split("交付文件：", 1)[1] and "deliver_file" in with_tool.split("交付文件：", 1)[1]
    assert "交付文件：" not in without


# --- 记录来自 Stage Runner 真正成功的 deliver_file ---


def tools(harness_: StageHarness, **results: Any) -> None:
    harness_.script.tools.update({"deliver_file": "write_reversible", "repo_edit": "write_reversible"})
    harness_.script.tool_results.update(results)


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_delivered_file_is_recorded_across_rounds(harness: StageHarness) -> None:  # noqa: F811
    s = harness.script
    tools(harness, deliver_file=lambda i: {"artifact_id": "art_1", "path": i.args["path"]})
    s.tool_steps["make"] = scripted_steps(
        ToolLoopStep(action="tool", tool="repo_edit", args={"path": "result.txt", "new": "42"}),
        ToolLoopStep(action="tool", tool="deliver_file", args={"path": "result.txt"}),
        ToolLoopStep(action="finish", result={"ok": True}),
    )
    s.tool_steps["more"] = scripted_steps(ToolLoopStep(action="finish", result={"ok": True}))
    s.plans["main"] = [
        {"seq": 1, "goal": "做文件", "exit_when": "完成", "nodes": [stage_goal("make", "tool_loop", tools=["repo_edit", "deliver_file"])]},
        {"seq": 2, "goal": "收尾", "exit_when": "完成", "nodes": [stage_goal("more", "tool_loop", tools=["repo_edit"]), stage_deliver(after=["more"])]},
    ]
    s.deliver["main"] = "answer"
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert s.review(inp.task_id, 1).verification["delivered"] == ["result.txt"]
    assert s.review(inp.task_id, 2).verification["delivered"] == ["result.txt"], "跨轮累计"


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_deliver_file_is_not_a_delivery(harness: StageHarness) -> None:  # noqa: F811
    s = harness.script
    tools(harness, deliver_file=lambda i: {"error": "工作区里没有 result.txt"})
    s.tool_steps["make"] = scripted_steps(ToolLoopStep(action="tool", tool="deliver_file", args={"path": "result.txt"}), ToolLoopStep(action="finish", result={"ok": True}))
    one_round(harness, stage_goal("make", "tool_loop", tools=["deliver_file"]), stage_deliver(after=["make"]))
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert "delivered" not in s.review(inp.task_id, 1).verification


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_a_blocked_claim_is_counted_for_the_next_review(harness: StageHarness) -> None:  # noqa: F811
    s = harness.script
    tools(harness)
    s.tool_steps["a"] = scripted_steps(ToolLoopStep(action="finish", result={"ok": True}))
    s.tool_steps["b"] = s.tool_steps["a"]
    s.plans["main"] = [
        {"seq": 1, "goal": "做", "exit_when": "完成", "nodes": [stage_goal("a", "tool_loop", tools=["repo_edit"])]},
        {"seq": 2, "goal": "再做", "exit_when": "完成", "nodes": [stage_goal("b", "tool_loop", tools=["repo_edit"]), stage_deliver(after=["b"])]},
    ]
    s.deliver["main"] = "answer"

    async def reviewer(inp: Any) -> dict[str, Any]:
        if inp.round_seq == 1:
            return {"verdict": "continue", "reason": "说交付了却没交付", "reason_code": "rule_done_undelivered"}
        return {"verdict": "done", "reason": "脚本：完成", "done": {"deliver_node": "answer"}}

    s.reviewer = reviewer
    inp, _ = await harness.run(effect_ceiling=enums_pb2.EFFECT_WRITE_REVERSIBLE)
    assert "delivery_blocked" not in s.review(inp.task_id, 1).verification
    assert s.review(inp.task_id, 2).verification["delivery_blocked"] == 1
