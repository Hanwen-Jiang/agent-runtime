"""TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 7 节验收：rollback 的结构化信号 regressions。

细化见该计划第 11.8 节：
- judge 类节点（role = judge）可以在节点输出里给 regressions：被这一轮改坏的既有产物（artifact、evidence，node_output.schema.json）。
  model 执行的 judge 由 Node Executor 按“{result, regressions}”的外壳向模型要输出，拆开后 result 照旧、regressions 另放；
  其他 role 不加外壳，给了 regressions 也丢掉。
- Stage Runner 把 regressions 放进 StageResult，只交给 Reviewer；下游节点只看得到 result。
- Reviewer 的 floor：有 regressions 时至少 rollback，退到最近的检查点；没有检查点或 rollback 已用满上限时问人。
  理由的代码是 rule_regression，参数列出被改坏的产物。
- 评测样本 rs_rollback_corrupted_accounts 要补上 regressions 才能由代码判对：那是评测集的标注改动，要人确认，不在这里改。
"""

from __future__ import annotations

from typing import Any

import pytest

from .executor_harness import ExecutorFixture, make_fixture, model
from .reviewer_support import HUMAN_REPLIES, atomic, cont, done, escalate_human, goal, ok, past, stage
from .stage_harness import StageHarness, harness  # noqa: F401 — harness 是 fixture
from .support import assert_valid
from .test_node_executor import run_model
from .test_reviewer_rules import decide
from .test_stage_runner import atomic as run_atomic
from .test_stage_runner import deliver, one_round, results

BROKEN = [{"artifact": "data/accounts.csv", "evidence": "上一轮检查点 120 行，本轮 0 行；test_accounts_loaded 失败"}]
JUDGE_SCHEMA = {"type": "object", "required": ["passed"], "properties": {"passed": {"type": "boolean"}, "feedback": {"type": "string"}}}


@pytest.fixture
def fx(tmp_path: Any) -> ExecutorFixture:
    return make_fixture(tmp_path)


# --- Node Executor：judge 的输出外壳 ---


async def test_a_model_judge_is_asked_for_result_and_regressions(fx: ExecutorFixture) -> None:
    fx.models.reply({"result": {"passed": False, "feedback": "账户被清空"}, "regressions": BROKEN})
    out = await run_model(fx, model("check", role="judge", output="score", output_schema=JUDGE_SCHEMA))
    assert out["result"] == {"passed": False, "feedback": "账户被清空"}
    assert out["regressions"] == BROKEN
    assert_valid("node_output", out)
    asked = fx.models.requests[0].generation.output_schema.value.struct_value.fields
    assert set(asked["properties"].struct_value.fields) == {"result", "regressions"}, "judge 按外壳要输出"


async def test_a_judge_without_regressions_has_none(fx: ExecutorFixture) -> None:
    fx.models.reply({"result": {"passed": True}})
    out = await run_model(fx, model("check", role="judge", output="score", output_schema=JUDGE_SCHEMA))
    assert out["result"] == {"passed": True} and "regressions" not in out


async def test_malformed_regressions_are_asked_once_more(fx: ExecutorFixture) -> None:
    fx.models.reply({"result": {"passed": False}, "regressions": [{"artifact": "data/accounts.csv"}]}, {"result": {"passed": False}, "regressions": BROKEN})
    out = await run_model(fx, model("check", role="judge", output="score", output_schema=JUDGE_SCHEMA))
    assert out["regressions"] == BROKEN and len(fx.models.requests) == 2


async def test_other_roles_are_not_wrapped(fx: ExecutorFixture) -> None:
    fx.models.reply({"passed": True})
    out = await run_model(fx, model("summary", output_schema=JUDGE_SCHEMA))
    asked = fx.models.requests[0].generation.output_schema.value.struct_value.fields
    assert set(asked["properties"].struct_value.fields) == {"passed", "feedback"}
    assert out["result"] == {"passed": True} and "regressions" not in out


# --- Stage Runner：只交给 Reviewer ---


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_regressions_reach_the_reviewer_but_not_downstream_nodes(harness: StageHarness) -> None:
    s = harness.script

    async def judged(inp: Any, _: Any) -> dict[str, Any]:
        return {"result": {"passed": False}, "regressions": BROKEN}

    async def summarized(inp: Any, _: Any) -> dict[str, Any]:
        return {"result": {"summary": "x"}, "regressions": BROKEN}

    s.behaviors["check"] = judged
    s.behaviors["summary"] = summarized
    one_round(
        harness,
        run_atomic("check", role="judge", effect="read"),
        run_atomic("summary", role="transform"),
        deliver("answer", after=["check", "summary"], inputs={"verdict": "$check.result", "summary": "$summary.result"}),
    )
    inp, _ = await harness.run()
    got = results(harness, inp.task_id)
    assert got["check"].regressions == BROKEN
    assert got["summary"].regressions == [], "只有 judge 的 regressions 算数"
    answer = next(c for c in s.executed(inp.task_id) if c.node_id == "answer").inp
    assert answer.node["inputs"]["verdict"] == {"passed": False}, "下游只拿到 result"


# --- Reviewer：至少 rollback ---

CHECKED = stage(goal("fix"), atomic("e2e", role="judge", after=("fix",)))


def broken_results() -> dict[str, Any]:
    return {"fix": ok(), "e2e": {**ok("judge", {"passed": False}), "regressions": BROKEN}}


def test_regressions_roll_back_to_the_latest_checkpoint() -> None:
    d, fl, _ = decide(CHECKED, broken_results(), cont(), history=[past(1, "continue"), past(2, "continue")])
    assert fl.step == "rollback"
    assert d.verdict["verdict"] == "rollback" and d.verdict["rollback"] == {"checkpoint": "round_2"} and d.raised
    assert d.verdict["reason_code"] == "rule_regression" and d.verdict["reason_params"] == {"artifacts": ["data/accounts.csv"]}


def test_regressions_cannot_be_done() -> None:
    d, _, _ = decide(CHECKED, broken_results(), done("e2e"), history=[past(1, "continue")])
    assert d.verdict["verdict"] == "rollback"


def test_without_a_checkpoint_regressions_ask_a_human() -> None:
    d, _, _ = decide(CHECKED, broken_results(), cont())
    assert d.verdict["verdict"] == "escalate" and d.verdict["escalate"]["target"] == "human"
    assert d.verdict["escalate"]["options"] == HUMAN_REPLIES and d.verdict["reason_code"] == "rule_regression"


def test_a_used_up_rollback_asks_a_human() -> None:
    history = [past(1, "continue"), past(2, "rollback", step="rollback")]
    d, _, _ = decide(CHECKED, broken_results(), cont(), history=history)
    assert d.verdict["verdict"] == "escalate" and d.verdict["escalate"]["target"] == "human"


def test_asking_a_human_is_already_above_rollback() -> None:
    d, _, _ = decide(CHECKED, broken_results(), escalate_human(), history=[past(1, "continue")])
    assert d.verdict["verdict"] == "escalate" and not d.raised

