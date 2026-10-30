"""Planner 的提示（计划 S-09 难点“缓存友好”）。

按 Context Zones 分区（附录 G.1）：

- system 区：Planner 的固定说明、输出的 JSON Schema 与 Node Catalog（按名字排序，含可用的 Tier）。与租户、Task、消息无关，
  同一个 Catalog 下逐字节相同；这条消息设缓存断点。
- brief 区：Triage 的结论、Session 上下文（已授权的风险、effect 上限、剩余预算、Recipe 缓存）与 Task Plan。
- 最后是本轮：用户消息、本轮序号、Task State、上一轮的 Stage、结果与 Verdict（replan 时含要保留的 Node）。

Context Builder（R-03）实现之后，由它按同样的分区组装。
"""

from __future__ import annotations

import json
from typing import Any

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_worker.planner import validate
from agentruntime_worker.planner.context import CatalogView, PlanningContext
from agentruntime_worker.planner.triage import MESSAGE_KINDS, TriageDecision
from agentruntime_worker.session.transcript import transcript_text

# 提示里放多少之前的对话（字）：分诊是小模型、只要看懂指代，规划要知道用户在接着说什么（TMP 计划第 11.13 节）。
CLASSIFIER_CONVERSATION_CHARS = 4000
PLANNING_CONVERSATION_CHARS = 16000

_PLANNER_RULES = """你是 Agent Runtime 的 Planner，在 Round 的开头决定这一轮做什么。只输出一个 JSON 对象：{"task_plan": …, "stage": …}，分别符合下面的 Task Plan 与 Stage 的 JSON Schema。

规则：
1. 一轮只规划到下一个信息检查点：要看了结果才能决定的后续步骤留到下一轮。信息齐全时可以一轮发出完整 DAG；几乎未知时只发一个探索用的 Goal Node。
2. Atomic Node 只能用下面 Node Catalog 列出的 Node Template，executor 与 effect 照模板写；其余写成 Goal Node（写清 goal、done_when、output_schema）。Recipe 与工具也只能用 Catalog 列出的。model 节点没有工具：读文件、跑命令、改代码、查外部数据，要放进一个带 tools 的 tool_loop Goal Node（或用 executor 为 program 的模板）；model 的 Atomic Node 只做加工、判断、汇总与交付，role 不能是 fetch 或 act。
3. Node 的 effect 不得超过 brief 区给出的 effect 上限；Goal Node 所列工具的 effect 不得高于它自己的 effect_ceiling。
4. 不可逆动作（effect 为 write_irreversible 的模板或工具）照实声明，运行时会在执行前插入人工审批；不要用 gate 节点替代，也不要降低 effect。
5. 最终答案只来自 role = deliver 的节点，只在信息齐全、可以交付的那一轮安排。
6. replan 只重做剩余部分：上一轮 Reviewer 保留的 Node 不要再安排，直接引用它们已有的结果。
7. 不要写 idempotency_key，也不必纠结 Stage 的 seq 与 Task Plan 的 triage.level：这三样由系统填写。Stage 的 goal、exit_when 与 Goal Node 的 goal、done_when 会显示给用户，用用户消息的语言写。
8. Task Plan 写清 horizon（full_dag、per_stage 或 explore）、上下文策略与预算；规划成本控制在 Task 预计成本的约 5% 以内。
9. Tier（model 节点的 tier 与 Task Plan 的 tiers）只能用 Node Catalog 列出的，写成 <模型档>@<推理强度>；拿不准就不写，由 Model Gateway 按 role 选默认 Tier。Triage 的级别（fast、light、full、replan）不是 Tier 名。Task State 的 constraints.model_preference 是用户的模型倾向：economy 尽量用便宜的 Tier，best 可以用最强的，balanced 或没给时按任务难度选。"""

_CLASSIFIER_RULES = """你给 Agent Runtime 的 Planner 做分诊。读用户这条消息（以及当前 Task 的摘要、本 Session 之前的对话），只输出一个 JSON 对象：
- kind：follow_up（续改上一个产出）、constraint（给运行中的 Task 加约束）、new_task（新任务）、question（简单问答）、chitchat（闲聊）、system_question（关于系统本身的问题）。
- task_family：新任务所属的任务族（形如 analytics.attribution），判断不了就填 null。
- confidence：你对 kind 与 task_family 的把握，0 到 1。
- effect：完成这条消息预计需要的最高 effect：none（只要生成文字）、read（要读外部数据）、write_reversible（可回滚的写）、write_irreversible（不可逆的外部动作，例如付款、发送）。要读工作区（用户的仓库与文件）才答得好的消息，effect 填 read，不要填 none：包括追问之前读过的仓库里的内容；只凭之前的对话就能答的仍是 none。
- irreversible_params_explicit：如果要做不可逆动作，它的对象与参数（金额、收件人、日期等）能否从这条消息里明确得出；不涉及不可逆动作时填 true。"""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _catalog_text(catalog: CatalogView) -> str:
    lines = ["Node Catalog："]
    lines += [f"- Node Template {name}：executor {executor}，effect {effect}" for name, (executor, effect) in sorted(catalog.templates.items())]
    lines += [f"- Recipe {name}：任务族 {family}" for name, family in sorted(catalog.recipes.items())]
    lines += [f"- 工具 {name}：effect {effect}" for name, effect in sorted(catalog.tools.items())]
    if catalog.tiers:
        lines.append(f"- Tier：{'、'.join(catalog.tiers)}")
    else:
        lines.append("- Tier：没有列出，不要写 tier（model 节点与 Task Plan 都不写），由 Model Gateway 按 role 选默认 Tier")
    return "\n".join(lines)


def system_zone(catalog: CatalogView) -> list[mg.ChatMessage]:
    schemas = "\n".join(f"{name} 的 JSON Schema：{_dump(validate.schema(name))}" for name in ("defs", "atomic_node", "goal_node", "stage", "task_plan"))
    text = f"{_PLANNER_RULES}\n\n{schemas}\n\n{_catalog_text(catalog)}"
    return [mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text=text, cache_breakpoint=True)]


def _brief(context: PlanningContext, decision: TriageDecision, task_plan: dict[str, Any] | None) -> str:
    lines = [
        f"Triage：{decision.level}（消息类别 {decision.kind}，任务族 {decision.task_family or '未知'}，置信度 {decision.confidence:.2f}）",
        f"判定依据：{'；'.join(decision.reasons) or '无'}",
        f"本 Session 已授权的风险等级：{context.risk_authorized}；effect 上限：{context.effect_ceiling}",
    ]
    if context.session_budget_remaining_usd is not None:
        lines.append(f"Session 剩余预算：${context.session_budget_remaining_usd:.2f}")
    if context.recipes:
        lines.append("Recipe 缓存：" + "；".join(f"{r.recipe}（{r.task_family}，风险 {r.risk}{'，最近失败过' if r.recent_failure else ''}）" for r in context.recipes))
    if task_plan:
        lines.append(f"之前的 Task Plan：{_dump(task_plan)}")
    return "\n".join(lines)


def _round(message: str, round_seq: int, task_state: dict[str, Any] | None, last_round: dict[str, Any] | None, keep_nodes: list[str] | None, conversation: str = "") -> str:
    lines = [f"用户消息：{message}", *([conversation] if conversation else []), f"本轮是第 {round_seq} 轮。"]
    if task_state:
        lines.append(f"Task State：{_dump(task_state)}")
        remaining = validate.task_budget_remaining(task_state)
        if remaining is not None:
            lines.append(f"Task 剩余预算：${remaining:.2f}（Stage 与各 Node 的 budget 都不要超过它，超过的会被压到剩余）")
    if last_round:
        verdict = last_round.get("verdict") or {}
        lines.append(f"上一轮的 Stage：{_dump(last_round.get('stage'))}")
        lines.append(f"上一轮的结果：{_dump(last_round.get('results'))}")
        lines.append(f"上一轮 Reviewer 的判定：{_dump(verdict)}")
        reply = last_round.get("human_reply") or {}
        if reply:
            lines.append(f"Reviewer 问了人，人的回复：{reply.get('option', '')}" + (f"；指导：{reply['guidance']}" if reply.get("guidance") else ""))
        repair = verdict.get("repair") or {}
        if repair.get("strategy") == "change_method":
            lines.append(f"要换方法重做的 Node：{'、'.join(repair.get('nodes') or [])}" + (f"；Reviewer 的指导：{repair['guidance']}" if repair.get("guidance") else ""))
        keep = keep_nodes if keep_nodes is not None else (verdict.get("replan") or {}).get("keep_nodes") or []
        if keep:
            lines.append(f"要保留的已验证 Node（不要再安排）：{'、'.join(keep)}")
        stuck = (verdict.get("replan") or {}).get("stuck_reason")
        if stuck:
            lines.append(f"上一次卡住的原因（这次要真正不同）：{stuck}")
    return "\n".join(lines)


def classifier_messages(message: str, task_state: dict[str, Any] | None, context: PlanningContext) -> list[mg.ChatMessage]:
    summary = ""
    if task_state:
        rounds = task_state.get("rounds") or []
        summary = f"\n当前 Task：目标 {task_state.get('goal', '')}；已有 {len(rounds)} 个 Round" + (f"，最近一个的判定是 {rounds[-1].get('verdict')}" if rounds and rounds[-1].get("verdict") else "")
    if context.task_families is not None:
        # TMP 计划第 6 节：任务族只能从词表里选，选不出就填 null（记为 general.unclassified）。
        listed = "\n".join(f"- {f.id}：{f.description}" + (f"（例如：{'；'.join(f.examples)}）" if f.examples else "") for f in context.task_families)
        hint = f"\ntask_family 只能从下面的词表里选一个，都不合适时填 null：\n{listed}"
    else:
        families = sorted({r.task_family for r in context.recipes})
        hint = f"\n已知的任务族：{'、'.join(families)}" if families else ""
    conversation = transcript_text(context.conversation, max_chars=CLASSIFIER_CONVERSATION_CHARS)
    session = (f"\n{conversation}" if conversation else "") + ("\n这个 Session 有工作区（用户的仓库与文件）。" if context.has_workspace else "")
    return [
        mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text=f"{_CLASSIFIER_RULES}\n可选的 kind：{'、'.join(MESSAGE_KINDS)}", cache_breakpoint=True),
        mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=f"用户消息：{message}{summary}{hint}{session}"),
    ]


def planning_messages(
    *,
    catalog: CatalogView,
    context: PlanningContext,
    decision: TriageDecision,
    message: str,
    task_state: dict[str, Any] | None,
    task_plan: dict[str, Any] | None,
    last_round: dict[str, Any] | None,
    round_seq: int,
    keep_nodes: list[str] | None = None,
) -> list[mg.ChatMessage]:
    return [
        *system_zone(catalog),
        mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text=_brief(context, decision, task_plan)),
        mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=_round(message, round_seq, task_state, last_round, keep_nodes, transcript_text(context.conversation, max_chars=PLANNING_CONVERSATION_CHARS))),
    ]


def retry_messages(previous: list[mg.ChatMessage], answer: str, problems: list[str]) -> list[mg.ChatMessage]:
    """重问：在原对话后附上模型的上次回答与不合格之处。"""
    feedback = "上次的回答不合格：\n" + "\n".join(f"- {p}" for p in problems) + "\n请修正后重新给出完整的 {\"task_plan\": …, \"stage\": …}。"
    return [*previous, mg.ChatMessage(role=mg.CHAT_ROLE_ASSISTANT, text=answer or "（没有回答）"), mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=feedback)]
