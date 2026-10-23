"""失败归因（附录 J.2，按 MAST 的三类；T-16）。

对没做成的 Task（aborted、failed；取消的不算这次做法的失败），用小模型（small@low）经 Model Gateway 判断属于哪一类：
specification（规格问题：目标或验收条件不清）、coordination（协作失配：Node 之间信息不一致）、verification（验证缺失：没有 judge 或太弱）。
输入只有结局、各 Round 的 verdict 与 Stage 形状（trace 里本来就没有用户数据），不带 task_id 与租户名。
输出按 schema 校验，不合格、调用失败、没有 Gateway 或预算用完时一律 unattributed。第一期归因只进报告。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, payload_pb2, tenant_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_learning.pipeline.budget import LearningBudget
from agentruntime_learning.pipeline.traces import TaskTrace

CATEGORIES = ("specification", "coordination", "verification")
UNATTRIBUTED = "unattributed"
FAILED_STATUSES = ("aborted", "failed")
TIER = "small@low"
# Learning 调用 Registry 与 Model Gateway 时的身份（docs/plan/learning-pipeline.md 第 4 节第 5 条）。
LEARNING_USER = "learning-pipeline"

OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["category", "reason"],
    "properties": {"category": {"enum": list(CATEGORIES)}, "reason": {"type": "string", "minLength": 1, "maxLength": 300}},
    "additionalProperties": False,
}

SYSTEM_PROMPT = """你在做多步 Agent 运行的失败归因。给你一次没做成的运行：最终结局、每一轮评审的结论与这一轮执行计划的结构。
从三类里选一类，并用一句话说明依据：
- specification：目标或验收条件不清，计划一开始就走偏或反复改方向；
- coordination：步骤之间信息不一致、上游结果没被下游正确使用、并行分支互相冲突；
- verification：缺少核验步骤或核验太弱，错误结果一路传到最后。
只输出 JSON：{"category": ..., "reason": ...}。"""


@dataclass(frozen=True)
class Attribution:
    task_id: str
    category: str
    reason: str = ""


def _prompt(task: TaskTrace) -> str:
    lines = [f"结局：{task.status}；共 {len(task.rounds)} 轮"]
    for r in task.rounds:
        shape = json.dumps(r.shape, ensure_ascii=False, sort_keys=True) if r.shape is not None else "（没有记录）"
        kind = "规划" if r.planned else "原地重跑"
        lines.append(f"第 {r.seq} 轮（{kind}）：评审结论 {r.verdict or '（无）'}；执行计划 {shape}")
    return "\n".join(lines)


def _parse(response: mg.GenerateResponse) -> dict[str, Any] | None:
    if response.HasField("result"):
        value = json_format.MessageToDict(response.result)
    else:
        try:
            value = json.loads(response.text)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict) or value.get("category") not in CATEGORIES or not isinstance(value.get("reason"), str):
        return None
    return value


def _request(task: TaskTrace, tenant_id: str, task_family: str, budget: LearningBudget) -> mg.GenerateRequest:
    schema = struct_pb2.Value()
    json_format.ParseDict(OUTPUT_SCHEMA, schema)
    return mg.GenerateRequest(
        tenant=tenant_pb2.TenantContext(tenant_id=tenant_id, user_id=LEARNING_USER),
        call=mg.CallContext(task_family=task_family, tier=TIER, role=enums_pb2.ROLE_JUDGE, max_cost_usd_micros=budget.remaining_micros),
        generation=mg.GenerateParams(
            messages=[mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text=SYSTEM_PROMPT), mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=_prompt(task))],
            output_schema=payload_pb2.JsonPayload(value=schema),
        ),
    )


async def attribute_failures(
    tasks: Iterable[TaskTrace], *, tenant_id: str, task_family: str, gateway: Any | None, budget: LearningBudget
) -> list[Attribution]:
    out: list[Attribution] = []
    for task in sorted((t for t in tasks if t.status in FAILED_STATUSES), key=lambda t: t.task_id):
        if gateway is None or budget.exhausted:
            out.append(Attribution(task.task_id, UNATTRIBUTED))
            continue
        try:
            response = await gateway.generate(_request(task, tenant_id, task_family, budget))
        except Exception:  # noqa: BLE001 — 归因失败不影响闭环，记 unattributed
            out.append(Attribution(task.task_id, UNATTRIBUTED, "模型调用失败"))
            continue
        budget.charge(response.usage.cost_usd_micros)
        parsed = _parse(response)
        if parsed is None:
            out.append(Attribution(task.task_id, UNATTRIBUTED, "模型输出不合 schema"))
        else:
            out.append(Attribution(task.task_id, parsed["category"], parsed["reason"]))
    return out
