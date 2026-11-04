"""MX-Q（docs/plan/all-plans-dag-2026-11-03.md 第 3 节）：能力矩阵的 plan、route 两格加上 public-adapted-drafts 里的改编题。

1. 计划里 `{"eval": "s09", "options": {"adapted": [...]}}` 打开；不打开（或给空的）时题目与评测集版本与原来完全一样。
2. 三套（natural-plan、travelplanner、swebench）两批草稿共 22 道原题；同一道原题派生的追问、replan 只取原题
   （public-adapted-drafts/README.md 第 8 条：不重复计为独立样本），题号带 `adapted:<套>/` 前缀。
3. 每道题用自己那套的 Catalog（swebench 的与 task-loop 的不同）；记录里写 `confirmed`（草稿都是 false）与 `adapted`。
4. 评测集版本把加进来的几套算进去：换了题，续跑会被拒（matrix 的 ResumeMismatch）。不认识的套名报错。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

import capability_support as cs
from agentruntime_learning.capability.base import PinningGateway
from agentruntime_learning.capability.evals import load_eval
from agentruntime_learning.evals.cases import DEFAULT_EVAL_SET, load_eval_set

from fake_gateway import ScriptedGateway

ADAPTED = ["natural-plan", "travelplanner", "swebench"]
ROOT = DEFAULT_EVAL_SET.parent / "public-adapted-drafts"
SETS = {name: load_eval_set(ROOT / name) for name in ADAPTED}


def reference_plan(request: mg.GenerateRequest) -> Any:
    """按提示里的用户消息在改编题里找到用例，答它的参照 Stage 与 Task Plan。几道题的消息互相包含时取最长的那道（最具体）。"""
    text = "\n".join(m.text for m in request.generation.messages)
    hits = [
        case for es in SETS.values() for case in es.task_cases
        if case.planner_input["message"] in text and case.planner_expect["triage"] in ("full", "replan")
    ]
    if not hits:
        return "?"
    ref = max(hits, key=lambda c: len(c.planner_input["message"])).planner_expect["reference"]
    return {"task_plan": cs.with_steps(ref["task_plan"]), "stage": ref["stage"]}


def ids(spec: str, options: dict[str, Any] | None = None) -> list[str]:
    return [i.id for i in load_eval(spec, options).items(1000, 0)]


@pytest.mark.parametrize("spec", ["s09", "s09:route"])
def test_without_adapted_sets_nothing_changes(spec: str) -> None:
    plain, empty = load_eval(spec), load_eval(spec, {"adapted": []})
    assert plain.version == empty.version and ids(spec) == ids(spec, {"adapted": []})
    assert not any(i.startswith("adapted:") for i in ids(spec))


@pytest.mark.parametrize("spec", ["s09", "s09:route"])
def test_the_cells_get_the_adapted_originals_once(spec: str) -> None:
    got = ids(spec, {"adapted": ADAPTED})
    adapted = [i for i in got if i.startswith("adapted:")]
    assert len(adapted) == 22, adapted
    assert {i.split("/", 1)[0] for i in adapted} == {f"adapted:{n}" for n in ADAPTED}
    assert not any(i.endswith(("_replan", "_followup_fast")) for i in adapted), "派生题只取原题"
    assert len(got) >= 40, "加上之后够出可发布的分数"
    assert set(ids(spec)) <= set(got), "原来的题都还在"
    assert load_eval(spec, {"adapted": ADAPTED}).version != load_eval(spec).version
    assert load_eval(spec, {"adapted": ADAPTED}).version != load_eval(spec, {"adapted": ADAPTED[:1]}).version


def test_items_stay_seeded_and_prefix_stable() -> None:
    ev = load_eval("s09", {"adapted": ADAPTED})
    ten, more = ev.items(10, 3), ev.items(40, 3)
    assert [i.id for i in more[:10]] == [i.id for i in ten]
    assert all(i.payload.get("adapted") == i.id.split("/", 1)[0].removeprefix("adapted:") for i in ev.items(1000, 0) if i.id.startswith("adapted:"))


def test_an_unknown_set_is_refused() -> None:
    with pytest.raises(ValueError, match="nope"):
        load_eval("s09", {"adapted": ["nope"]})


async def test_adapted_questions_use_their_own_catalog_and_say_they_are_unconfirmed() -> None:
    ev = load_eval("s09", {"adapted": ADAPTED})
    items = [i for i in ev.items(1000, 0) if i.id.startswith("adapted:")]
    assert items
    for item in items:
        inner = cs.ServingGateway(ScriptedGateway(plan=reference_plan))
        pin = PinningGateway(inner, role="plan", candidate=cs.OPUS, tier="strong@medium")
        result = await ev.run_item(item, pin, candidate=cs.OPUS)
        assert result.record["confirmed"] is False and result.record["adapted"] == item.payload["adapted"]
        assert result.score == 1.0, (item.id, result.record["failures"])
        assert not pin.contaminated
