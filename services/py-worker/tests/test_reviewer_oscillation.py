"""S-10 验收“震荡测试”：构造来回失败的场景，断言在上限内终止（计划 S-10 验收与难点“防震荡”）。

闭环模拟 Task Loop：每一轮的 Stage 都失败；Reviewer 配上对抗性的假模型（永远原地重试、repair 与 replan 来回切换、
永远判 continue、永远 replan、永远换更强的 Tier、永远判 done、模型一直出错）。原地重试与升级 Tier 重跑同一个 Stage
（Node id 不变）；其余判定之后换一批 Node（id 变了，按 Node 计的失败次数从头算，最不利于终止）。问人时假定人选
“带指导重规划”。每轮的判定按 Worker 的格式记进历史，交给下一轮。
"""

from __future__ import annotations

import dataclasses
import itertools
from collections import Counter
from typing import Any

import pytest

from agentruntime_worker.reviewer.reviewer import Reviewer, ReviewRequest
from agentruntime_worker.reviewer.rules import Caps
from agentruntime_worker.reviewer.signals import history_entry, step_of

from .reviewer_support import (
    CATALOG,
    TASK_ID,
    TENANT,
    FakeReviewGateway,
    always,
    atomic,
    cont,
    done,
    escalate_tier,
    failed,
    replan,
    retry,
    skipped,
    stage,
    task_state,
)

CAPS = Caps()
BOUND = 1 + sum(dataclasses.asdict(CAPS).values())
"""每一级用满上限之后只剩 abort：评审次数不超过各级上限之和加一。"""


def alternating() -> Any:
    toggle = itertools.cycle([retry("ghost"), replan()])
    return lambda _request: next(toggle)


ADVERSARIES = {
    "always_retry": lambda: always(retry("ghost")),
    "repair_replan_back_and_forth": alternating,
    "always_continue": lambda: always(cont()),
    "always_replan": lambda: always(replan()),
    "always_stronger_tier": lambda: always(escalate_tier()),
    "always_done": lambda: always(done("ghost")),
    "model_down": lambda: always_error,
}


def always_error(_request: Any) -> BaseException:
    return RuntimeError("模型不可用")


async def simulate(model: Any, *, max_rounds: int = 50) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reviewer = Reviewer(FakeReviewGateway(model))
    history: list[dict[str, Any]] = []
    done_rounds: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    generation = 0
    for seq in range(1, max_rounds + 1):
        fetch, analyze = f"fetch_{generation}", f"analyze_{generation}"
        s = stage(atomic(fetch, role="fetch", template="sql_query@v1", executor="program", effect="read"), atomic(analyze, after=(fetch,)), seq=seq, goal_text="取数并归因", exit_when="归因完成")
        results = {fetch: failed("数据源返回 500", role="fetch"), analyze: skipped(fetch)}
        state = task_state(rounds=[*done_rounds, {"seq": seq, "stage_goal": "取数并归因", "status": "reviewing"}])
        result = await reviewer.review(
            ReviewRequest(tenant=TENANT, task_id=TASK_ID, round_seq=seq, task_state=state, stage=s, results=results, history=list(history), catalog=CATALOG)
        )
        verdict = result.verdict
        verdicts.append(verdict)
        history.append(history_entry(seq, verdict, results))
        done_rounds.append({"seq": seq, "stage_goal": "取数并归因", "status": "completed", "verdict": verdict["verdict"]})
        if verdict["verdict"] in ("abort", "done"):
            break
        same_stage = step_of(verdict) == "repair_retry" or (verdict["verdict"] == "escalate" and verdict["escalate"]["target"] == "tier")
        if not same_stage:
            generation += 1
    return verdicts, history


@pytest.mark.parametrize("adversary", sorted(ADVERSARIES))
async def test_a_task_that_keeps_failing_ends_in_abort_within_the_caps(adversary: str) -> None:
    verdicts, _ = await simulate(ADVERSARIES[adversary]())
    kinds = [v["verdict"] for v in verdicts]
    assert kinds[-1] == "abort", f"{adversary}：没有在上限内收尾：{kinds}"
    assert len(verdicts) <= BOUND, f"{adversary}：评审了 {len(verdicts)} 次，超过上限 {BOUND}：{kinds}"
    assert "done" not in kinds and "continue" not in kinds, "Stage 全部失败时不能判 done 或 continue"


@pytest.mark.parametrize("adversary", sorted(ADVERSARIES))
async def test_no_step_is_used_more_often_than_its_cap(adversary: str) -> None:
    verdicts, _ = await simulate(ADVERSARIES[adversary]())
    steps = Counter(step_of(v) for v in verdicts)
    targets = Counter(v["escalate"]["target"] for v in verdicts if v["verdict"] == "escalate")
    assert steps["repair_retry"] <= CAPS.repair_retry
    assert steps["repair_change_method"] <= CAPS.repair_change_method
    assert steps["replan"] <= CAPS.replan
    assert steps["replan_change_shape"] <= CAPS.replan_change_shape
    assert steps["rollback"] <= CAPS.rollback
    assert targets["tier"] <= CAPS.escalate_tier and targets["human"] <= CAPS.escalate_human


@pytest.mark.parametrize("adversary", sorted(ADVERSARIES))
async def test_no_node_is_repaired_on_its_third_failure(adversary: str) -> None:
    verdicts, history = await simulate(ADVERSARIES[adversary]())
    failures: Counter[str] = Counter()
    for verdict, entry in zip(verdicts, history, strict=True):
        failures.update(entry.get("failed_nodes", {}).keys())
        if verdict["verdict"] == "repair":
            worst = max(failures[n] for n in entry.get("failed_nodes", {}))
            assert worst < 3, f"{adversary}：第 {entry['round']} 轮，Node 已失败 {worst} 次还在 repair"
            if worst == 2:
                assert verdict["repair"]["strategy"] == "change_method", "失败 2 次就换方法"


async def test_the_escalation_ladder_climbs_in_b8_order_when_the_model_always_retries() -> None:
    """模型永远原地重试时，代码按附录 B.8 的顺序逐级往上走：repair → replan → 换形状 → 更强的 Tier → 问人 → abort。"""
    verdicts, _ = await simulate(always(retry("ghost")))
    order = ["repair_retry", "repair_change_method", "replan", "replan_change_shape", "escalate", "abort"]
    seen = [step_of(v) for v in verdicts]
    firsts = [seen.index(step) for step in order if step in seen]
    assert firsts == sorted(firsts), seen
    assert {"replan", "replan_change_shape", "escalate", "abort"} <= set(seen), seen
