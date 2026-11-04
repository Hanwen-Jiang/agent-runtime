"""Progress Check 的提示（附录 B.6；计划 S-10 交付“Progress Check 提示”、难点“上下文隔离”）。

- system 区：Reviewer 的固定说明（判定表、四层诊断、典型症状、升级顺序）与输出的 JSON Schema，与租户、Task 无关，
  逐字节相同；这条消息设缓存断点。
- 本轮的材料只由 review_view 按白名单取出：Task State 的规定字段、本轮执行的 Stage、Stage 结果、之前各 Round 的摘要，
  外加代码算出的硬信号、floor、预算、可退回的检查点与可换的 Recipe。Task Plan、Planner 与 Node 的推理文本、
  Triage 的理由都不在白名单里，到不了模型——上下文隔离由代码保证，不靠提示约束。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_worker.planner import validate
from agentruntime_worker.reviewer.rules import HUMAN_REPLIES, Floor, ReviewCatalog
from agentruntime_worker.reviewer.signals import Ledger

REVIEW_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "object"},
        "completion": {"type": "number", "minimum": 0, "maximum": 1},
        "state_update": {"type": "object"},
        "highlights": {"type": "object"},
    },
}
"""Progress Check 的结构化输出外层；Verdict 的完整 schema 放在 system 区（模型网关不解析 $ref）。"""

_REVIEWER_RULES = f"""你是 Agent Runtime 的 Reviewer，在 Round 的结尾判定下一步。你只看本轮的结果、Task State 与验收条件。只输出一个 JSON 对象：{{"verdict": …, "completion": …, "state_update": …, "highlights": …}}
- verdict：符合下面 Verdict 的 JSON Schema。问人时 options 只能是 {"、".join(HUMAN_REPLIES)} 三种。
- completion：整个 Task 的完成度，0 到 1，看产出物本身。
- state_update：要写进 Task State 的汇总：facts（本轮已验证的事实）、assumptions_verified 与 assumptions_broken（照抄 Task State 里假设的原文）、open_questions（待查的问题）。只写有根据的，各几条即可。facts 只写关于任务与产出物本身的事实（例如仓库里有哪些文件、测试结果），不写判定的过程，不提 floor、硬信号、规则这些给你的内部材料。
- highlights：这一轮给人看的重要输出：files（重要文件，每项 path 与 why，why 一句话说明为什么重要；path 只写这一轮工具调用碰过的文件）、answers（重要答案，每条不超过 300 字），各最多 5 条。没有就省略。
- guidance：Task State 里的 guidance 是用户在运行中发来的纠偏，按它验收：产出不符合用户的纠偏就不算完成。
- 子 Agent 的来源卡片（sub_agent 交回的 cards，每张 claim、evidence、source、confidence）：几个子 Agent 的卡片结论互相冲突、综合节点却没有提到时，判 repair 综合节点（协作失配），guidance 写明是哪几张卡片冲突。
- verification：代码记下的事实（wrote 改过文件没有，ran_after_write 改完运行过命令没有，last_exit_code 最后一次命令的退出码）。Node 的结果是模型写的，这份不是，两者矛盾时信它。last_exit_code 非零时要分清是这次改坏的，还是已有的失败：看本轮的结果，说不清就不要判 done。delivered 是经 deliver_file 真正交付给用户的文件（没有这一项就是一个也没交付）：交付文件以它为准，回答说交付了、这里却没有的就是没交付；用户要文件而还没交付时不要判 done，让下一轮用 deliver_file 交付。
- 语言：给人看的文字（verdict 的 reason、问人的 question 与 state_summary、repair 的 guidance、replan 的 stuck_reason、state_update 与 highlights 的各条）用用户的语言写，即 Task State 里 goal 所用的语言（goal 是英文就写英文）。

先回答 Progress Check 的五个问题，写进 verdict.progress_check：是否已满足验收条件？最近几个 Round 有没有实质进展？是否在绕圈？计划依赖的假设还成立吗？下一步做什么、由谁做？

判定（附录 B.4）：
- done：满足 Task 的验收条件 done_when，并且本轮有成功的 deliver 节点；done.deliver_node 写那个节点。是否完成看产出物本身。
- continue：本 Stage 的退出条件 exit_when 已满足，Task 尚未完成。
- repair：个别 Node 失败，属于执行层问题。retry_with_error 带上错误重试；change_method 目标不变，换工具、换路径、把 Node 拆小或先补充信息。nodes 写要修的 Node（judge 不通过时修它检查的上游产出）；guidance 写具体怎么修。
- replan：计划依赖的假设被证伪，或出现停滞。keep_nodes 写本轮成功、不必重做的 Node；stuck_reason 写卡住的原因，让下一次尝试真正不同；Stage 形状本身不合适时 change_shape 为 true。
- rollback：共享产物被改坏，退回材料里列出的检查点之一。
- reroute：任务实际属于另一类流程，换成材料里列出的 Recipe 之一。
- escalate：卡住，或遇到高风险决定。target 为 tier 时换更强的 Tier；为 human 时带着 state_summary 和一个具体的问题去问人。
- abort：预算用尽，或确认无法完成；写清完成了什么、没完成什么。

四层诊断（附录 B.5），问题所在的层写进 verdict.layer：execution（工具报错、输出格式不对，默认 repair）；plan（前提假设被证伪、某个 Node 做不完，默认 replan）；shape（Stage 形状不合适，默认 replan 换形状或 reroute）；goal（产出偏离真实需求，默认 escalate 回到澄清）。

形状不对的典型症状（附录 B.7）：扇出后子结果冲突 → 换形状，收回单一写者；前几个 Node 就频繁 replan → 先探索再规划；tool_loop 轨迹很长、没有里程碑 → 补一个显式计划；revise 里同一错误反复出现 → 问题在上游，停止修复，replan；反思只改措辞 → 换方法，引入外部判据或独立 judge；ensemble 分歧大 → 澄清或拆解；辩论很快一致或永不收敛 → 给 judge 明确 rubric，强制安排反方；judge 总选第一个或最长的 → 两两比较并交换顺序；上下文越来越长、质量下降 → 换方法：状态写入 Task State 后 reset。触发的硬信号写进 verdict.signals。

升级顺序（附录 B.8）：原地重试 → 换方法 → replan → 换形状或 reroute → rollback → escalate → abort。最常见的错误是用低层手段处理高层问题，例如计划本身错了却原地重试。同一 Node 失败 2 次就换方法，3 次就 replan。材料里的 floor 是代码按规则算出的“至少要升级到哪一步”，低于它的判定会被改写。"""

_DEFS = ("verdict", "non_empty_string", "diagnosis_layer", "hard_signal", "node_id", "repair_strategy", "escalate_target", "tier", "human_reply", "recipe_ref", "versioned_ref")

_TASK_STATE_KEYS = ("task_id", "session_id", "goal", "done_when", "status", "constraints", "milestones", "facts", "assumptions", "open_questions", "guidance", "rounds", "artifacts")
_ROUND_KEYS = ("seq", "stage_goal", "triage_level", "status", "verdict", "cost_usd")
_STAGE_KEYS = ("seq", "goal", "exit_when", "budget", "nodes")
_NODE_KEYS = (
    "id", "kind", "template", "executor", "output", "effect", "role", "after", "tier", "reasoning", "quality", "inputs",
    "goal", "done_when", "mode", "tools", "recipe", "effect_ceiling", "output_schema", "max_iterations", "budget", "retry",
)  # fmt: skip
_RESULT_KEYS = ("role", "status", "result", "failure_class", "error", "quality", "attempts", "cost_usd")
_HISTORY_KEYS = ("round", "verdict", "step", "escalate", "failed_nodes")
_DROPPED = "rationale"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _pick(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: _scrub(value[k]) for k in keys if isinstance(value, Mapping) and k in value}


def _scrub(value: Any) -> Any:
    """去掉任何层级里的推理文本字段：Node 的推理只写入 trace，不传给下游。"""
    if isinstance(value, Mapping):
        return {k: _scrub(v) for k, v in value.items() if k != _DROPPED}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def review_view(task_state: Mapping[str, Any], stage: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]], history: list[Mapping[str, Any]]) -> dict[str, Any]:
    """交给 Reviewer 的材料（白名单）。"""
    state = _pick(task_state, _TASK_STATE_KEYS)
    if "rounds" in state:
        state["rounds"] = [_pick(r, _ROUND_KEYS) for r in task_state.get("rounds") or []]
    view_stage = _pick(stage, _STAGE_KEYS)
    view_stage["nodes"] = [_pick(n, _NODE_KEYS) for n in stage.get("nodes") or []]
    return {
        "task_state": state,
        "stage": view_stage,
        "results": {nid: _pick(r, _RESULT_KEYS) for nid, r in sorted(results.items())},
        "history": [_pick(h, _HISTORY_KEYS) for h in history],
    }


def system_zone() -> list[mg.ChatMessage]:
    defs = validate.schema("defs")["$defs"]
    schemas = f"Verdict 的 JSON Schema：{_dump(validate.schema('verdict'))}\n其中引用的定义（defs.schema.json）：{_dump({k: defs[k] for k in _DEFS if k in defs})}"
    return [mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text=f"{_REVIEWER_RULES}\n\n{schemas}", cache_breakpoint=True)]


def review_messages(view: dict[str, Any], *, ledger: Ledger, floor: Floor, catalog: ReviewCatalog | None, verification: Mapping[str, Any] | None = None) -> list[mg.ChatMessage]:
    findings: dict[str, Any] = {
        "signals": list(ledger.signals),
        "floor": floor.step,
        "floor_reasons": list(floor.reasons),
        "checkpoints": list(ledger.checkpoints),
    }
    ratio = ledger.spent_ratio()
    if ratio is not None:
        findings["budget"] = {"spent_usd": round(ledger.spent_usd, 6), "budget_usd": ledger.budget_usd, "spent_ratio": round(ratio, 4)}
    if catalog is not None:
        findings["recipes"] = {name: risk for name, risk in sorted(catalog.recipes.items())}
        findings["effect_ceiling"] = catalog.effect_ceiling
    if verification:
        findings["verification"] = dict(verification)
    text = f"本轮的材料：{_dump(view)}\n代码算出的硬信号、floor 与可选项：{_dump(findings)}"
    return [*system_zone(), mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=text)]


def checked_highlights(raw: Any) -> dict[str, Any] | None:
    """模型给的 highlights：形状不对就整个不要（给人看的，判定才要紧，不为它重问）。路径的核对与截断在工作流里（workflows/highlights.py）。"""
    if not isinstance(raw, Mapping):
        return None
    files, answers = raw.get("files", []), raw.get("answers", [])
    if not isinstance(files, list) or not all(isinstance(f, Mapping) and isinstance(f.get("path"), str) and f["path"].strip() and isinstance(f.get("why", ""), str) for f in files):
        return None
    if not isinstance(answers, list) or not all(isinstance(a, str) for a in answers):
        return None
    return dict(raw) if files or answers else None


def retry_messages(previous: list[mg.ChatMessage], answer: str, problems: list[str]) -> list[mg.ChatMessage]:
    """重问：在原对话后附上模型的上次回答与不合格之处。"""
    feedback = "上次的回答不合格：\n" + "\n".join(f"- {p}" for p in problems) + '\n请修正后重新给出完整的 {"verdict": …, "completion": …, "state_update": …}。'
    return [*previous, mg.ChatMessage(role=mg.CHAT_ROLE_ASSISTANT, text=answer or "（没有回答）"), mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=feedback)]


def answer_problems(answer: Any) -> list[str]:
    """Progress Check 的回答哪里不合格；合格时返回空列表。"""
    if not isinstance(answer, dict):
        return ['回答不是 JSON 对象：要按输出格式给出 {"verdict": …, "completion": …, "state_update": …}']
    verdict = answer.get("verdict")
    if not isinstance(verdict, dict):
        return ["缺少 verdict 对象"]
    found = validate.schema_problems("verdict", verdict)
    completion = answer.get("completion")
    if completion is not None and (not isinstance(completion, int | float) or isinstance(completion, bool) or not 0 <= completion <= 1):
        found.append(f"completion 要是 0 到 1 之间的数，得到 {completion!r}")
    if "state_update" in answer and not isinstance(answer["state_update"], dict):
        found.append("state_update 要是 JSON 对象")
    return found
