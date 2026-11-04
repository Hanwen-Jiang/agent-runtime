"""N16-R 第二段（docs/plan/sub-agent.md 第 5 节）：子 Agent 交回来源卡片，合并时看得出冲突。

1. 开关打开、brief 的 output 要来源卡片、Planner 没给具体的 output_schema（没写或只写了 {"type": "object"}）时，
   代码给这个 sub_agent 填上卡片的 schema：{"cards": [{claim, evidence, source, confidence, open_questions}]}。
   结构化输出在 tool_loop 的 activity 里按它校验，不合格带改正说明重问（现有逻辑），不改命令序列。给了具体 schema 的、
   brief 不要卡片的、不是 sub_agent 的，都不动；开关关着时不动。
2. Reviewer：互不依赖的 sub_agent 里有交回零张卡片的，与交回空结果一样算 parallel_anomaly。
3. Progress Check 的提示写明：卡片之间结论冲突、综合节点没提到时判 repair（附录 B.7 协作失配）。
"""

from __future__ import annotations

from typing import Any

from agentruntime_worker.executor.schemas import validate as validate_schema
from agentruntime_worker.planner import validate
from agentruntime_worker.planner.planner import Planner, PlannerConfig
from agentruntime_worker.reviewer import prompts as reviewer_prompts
from agentruntime_worker.reviewer.signals import ledger

from .planner_support import FakeGateway, plan_answer, request
from .reviewer_support import goal as review_goal
from .reviewer_support import ok, stage as review_stage, task_state
from .test_n04_task_plan import RISKY_FULL
from .test_planner_sub_agent import BRIEF
from .test_stage_runner import deliver, goal, stage

CARDS = {**BRIEF, "output": "来源卡片的数组"}


def agent(node_id: str, brief: dict[str, Any] = CARDS, output_schema: Any = None) -> dict[str, Any]:
    node = goal(node_id, "sub_agent", effect_ceiling="read", tools=["web_search"], inputs={"brief": brief})
    if output_schema is None:
        node.pop("output_schema")
    else:
        node["output_schema"] = output_schema
    return node


def schemas_of(plan: dict[str, Any]) -> dict[str, Any]:
    return {n["id"]: n.get("output_schema") for n in plan["nodes"]}


def test_sub_agents_asked_for_cards_get_the_card_schema() -> None:
    plan = stage(
        1,
        agent("a", output_schema={"type": "object"}),
        agent("b"),
        agent("c", output_schema="result_v1"),
        agent("d", brief={**BRIEF, "output": "一段三句话的摘要"}, output_schema={"type": "object"}),
        deliver(after=["a", "b", "c", "d"]),
    )
    filled, notes = validate.card_outputs(plan)
    got = schemas_of(filled)
    assert got["a"] == validate.SOURCE_CARDS_SCHEMA and got["b"] == validate.SOURCE_CARDS_SCHEMA
    assert got["c"] == "result_v1", "Planner 给了具体的 schema 就照它"
    assert got["d"] == {"type": "object"}, "brief 不要卡片的不动"
    assert got["answer"] == schemas_of(plan)["answer"] and notes
    assert schemas_of(plan)["a"] == {"type": "object"}, "不改原来的 Stage"
    english, _ = validate.card_outputs(stage(1, agent("e", brief={**BRIEF, "output": "an array of source cards"}), deliver(after=["e"])))
    assert schemas_of(english)["e"] == validate.SOURCE_CARDS_SCHEMA


def test_the_card_schema_checks_each_card() -> None:
    good = {"cards": [{"claim": "A 支持 30 天退款", "evidence": "政策页第 2 段", "source": "https://a.example/refund", "confidence": 0.8, "open_questions": []}]}
    assert not validate_schema(validate.SOURCE_CARDS_SCHEMA, good)
    assert not validate_schema(validate.SOURCE_CARDS_SCHEMA, {"cards": []}), "没查到也可以交回空的"
    assert validate_schema(validate.SOURCE_CARDS_SCHEMA, {"cards": [{"evidence": "x", "source": "y", "confidence": 0.5}]}), "claim 必填"
    assert validate_schema(validate.SOURCE_CARDS_SCHEMA, {"cards": [{**good["cards"][0], "confidence": 2}]}), "confidence 在 0～1"


async def test_the_planner_fills_the_card_schema_only_with_the_rules_on() -> None:
    answer = plan_answer(agent("a", output_schema={"type": "object"}), deliver(after=["a"]))
    on = await Planner(FakeGateway(triage=RISKY_FULL, plan=[answer]), PlannerConfig(sub_agent_rules=True)).plan(request(round_seq=1))
    assert schemas_of(on.stage)["a"] == validate.SOURCE_CARDS_SCHEMA and not on.fallback
    off = await Planner(FakeGateway(triage=RISKY_FULL, plan=[plan_answer(agent("a", output_schema={"type": "object"}), deliver(after=["a"]))])).plan(request(round_seq=1))
    assert schemas_of(off.stage)["a"] == {"type": "object"}


def test_zero_cards_count_as_an_empty_result() -> None:
    s = review_stage(review_goal("market", mode="sub_agent"), review_goal("pricing", mode="sub_agent"))
    lg = ledger(task_state(), s, {"market": ok(result={"cards": [{"claim": "x"}]}), "pricing": ok(result={"cards": []})}, [])
    assert "parallel_anomaly" in lg.signals
    fine = ledger(task_state(), s, {"market": ok(result={"cards": [{"claim": "x"}]}), "pricing": ok(result={"cards": [{"claim": "y"}]})}, [])
    assert "parallel_anomaly" not in fine.signals


def test_the_progress_check_is_told_about_card_conflicts() -> None:
    rules = reviewer_prompts._REVIEWER_RULES
    assert "卡片" in rules and "综合节点" in rules and "repair" in rules.split("卡片", 1)[1]
