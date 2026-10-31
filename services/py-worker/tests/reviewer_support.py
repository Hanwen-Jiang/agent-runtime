"""S-10 测试共用：按脚本作答 Progress Check 的假 Model Gateway，Stage、结果、Task State 与评审请求的构造。"""

from __future__ import annotations

from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import metering_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.reviewer.reviewer import ReviewRequest
from agentruntime_worker.reviewer.rules import ReviewCatalog

TASK_ID = "tsk_01J" + "0" * 22 + "R"
SESSION_ID = "ses_01J" + "0" * 22 + "S"
TENANT = Tenant(tenant_id="t1", user_id="u1", session_id=SESSION_ID)
HUMAN_REPLIES = ["continue_as_is", "replan_with_guidance", "stop_and_wrap_up"]

CATALOG = ReviewCatalog(
    recipes={"ticket_triage@v4": "write_reversible", "refund_analysis@v3": "read", "flight_booking@v1": "write_irreversible"},
    effect_ceiling="write_reversible",
)


# --- Stage 与结果 ---


def atomic(node_id: str, *, role: str = "transform", template: str = "answer@v1", executor: str = "model", effect: str = "none", after: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "kind": "atomic", "template": template, "executor": executor, "effect": effect, "role": role}
    if executor == "model":
        node["output"] = "generate"
    if after:
        node["after"] = list(after)
    node.update(extra)
    return node


def goal(node_id: str, *, mode: str = "tool_loop", after: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "kind": "goal", "goal": f"完成 {node_id}", "done_when": f"{node_id} 给出结果", "mode": mode, "output_schema": {"type": "object"}}
    if after:
        node["after"] = list(after)
    node.update(extra)
    return node


def stage(*nodes: dict[str, Any], seq: int = 1, goal_text: str = "修复 lint", exit_when: str = "lint 通过") -> dict[str, Any]:
    return {"seq": seq, "goal": goal_text, "exit_when": exit_when, "nodes": list(nodes)}


def ok(role: str = "transform", result: Any = None, *, cost: float = 0.0) -> dict[str, Any]:
    return {"role": role, "status": "succeeded", "result": {"ok": True} if result is None else result, "cost_usd": cost}


def failed(error: str, *, role: str = "transform", failure_class: str = "invalid_output", status: str = "failed") -> dict[str, Any]:
    return {"role": role, "status": status, "failure_class": failure_class, "error": error}


def skipped(upstream: str, *, role: str = "transform") -> dict[str, Any]:
    return {"role": role, "status": "skipped", "failure_class": "upstream_failed", "error": f"上游 {upstream} 没有成功"}


# --- Task State 与历史 ---


def task_state(
    *,
    rounds: list[dict[str, Any]] | None = None,
    budget: float | None = None,
    done_when: str = "lint 通过",
    facts: list[str] | None = None,
    assumptions: list[str] | None = None,
    ceiling: str = "write_reversible",
) -> dict[str, Any]:
    constraints: dict[str, Any] = {"effect_ceiling": ceiling}
    if budget is not None:
        constraints["budget_usd"] = budget
    return {
        "task_id": TASK_ID,
        "session_id": SESSION_ID,
        "goal": "修好 CI 里的 lint 失败",
        "done_when": done_when,
        "status": "running",
        "constraints": constraints,
        "milestones": [],
        "facts": list(facts or []),
        "assumptions": list(assumptions or []),
        "open_questions": [],
        "rounds": rounds if rounds is not None else [{"seq": 1, "stage_goal": "修复 lint", "status": "reviewing"}],
        "artifacts": [],
    }


def rounds(*verdicts: str, costs: list[float] | None = None, current_cost: float | None = None, triage: str | None = None) -> list[dict[str, Any]]:
    """之前各 Round 的记录（按给出的 Verdict），加上正在评审的这一轮。"""
    out: list[dict[str, Any]] = []
    for i, v in enumerate(verdicts, start=1):
        entry: dict[str, Any] = {"seq": i, "stage_goal": "修复 lint", "status": "completed", "verdict": v}
        if costs:
            entry["cost_usd"] = costs[i - 1]
        out.append(entry)
    current: dict[str, Any] = {"seq": len(verdicts) + 1, "stage_goal": "修复 lint", "status": "reviewing"}
    if current_cost is not None:
        current["cost_usd"] = current_cost
    if triage is not None:
        current["triage_level"] = triage
    out.append(current)
    return out


def past(round_seq: int, verdict: str, *, step: str | None = None, failed_nodes: dict[str, str] | None = None, escalate: str | None = None) -> dict[str, Any]:
    """历史里的一轮（S-08 的 history_round，Worker 另带 step 与 escalate 的去向）。"""
    entry: dict[str, Any] = {"round": round_seq, "verdict": verdict}
    if step is not None:
        entry["step"] = step
    if escalate is not None:
        entry["escalate"] = escalate
    if failed_nodes:
        entry["failed_nodes"] = dict(failed_nodes)
    return entry


def request(
    stage_: dict[str, Any],
    results: dict[str, dict[str, Any]],
    *,
    history: list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    catalog: ReviewCatalog | None = CATALOG,
) -> ReviewRequest:
    state = state if state is not None else task_state(rounds=rounds(*[h["verdict"] for h in history or []]))
    return ReviewRequest(
        tenant=TENANT,
        task_id=TASK_ID,
        round_seq=len(state.get("rounds") or [1]),
        task_state=state,
        stage=stage_,
        results=results,
        history=list(history or []),
        catalog=catalog,
    )


# --- 模型的回答 ---


def answer(verdict: dict[str, Any], *, completion: float = 0.5, state_update: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """Progress Check 的结构化输出：Verdict（verdict.schema.json）、完成度与要写进 Task State 的汇总。"""
    verdict = dict(verdict)
    verdict.setdefault("reason", "模型的判断")
    verdict.setdefault(
        "progress_check",
        {"criteria_met": verdict["verdict"] == "done", "progress": True, "looping": False, "assumptions_hold": True, "next_step": "按判定继续"},
    )
    return {"verdict": verdict, "completion": completion, "state_update": state_update or {}, **extra}


def retry(*nodes: str) -> dict[str, Any]:
    return answer({"verdict": "repair", "repair": {"nodes": list(nodes), "strategy": "retry_with_error"}})


def change_method(*nodes: str, guidance: str = "换一种做法") -> dict[str, Any]:
    return answer({"verdict": "repair", "repair": {"nodes": list(nodes), "strategy": "change_method", "guidance": guidance}})


def replan(*, keep: list[str] | None = None, change_shape: bool = False, stuck: str = "计划依赖的假设不成立") -> dict[str, Any]:
    details: dict[str, Any] = {"stuck_reason": stuck, "change_shape": change_shape}
    if keep is not None:
        details["keep_nodes"] = keep
    return answer({"verdict": "replan", "replan": details})


def escalate_human(question: str = "要不要继续？", options: list[str] | None = None) -> dict[str, Any]:
    return answer({"verdict": "escalate", "escalate": {"target": "human", "question": question, "options": options or list(HUMAN_REPLIES), "state_summary": "已做的与卡住的地方"}})


def escalate_tier(tier: str = "strong@high") -> dict[str, Any]:
    return answer({"verdict": "escalate", "escalate": {"target": "tier", "tier": tier}})


def done(deliver: str, *, completion: float = 1.0) -> dict[str, Any]:
    return answer({"verdict": "done", "done": {"deliver_node": deliver}}, completion=completion)


def cont(*, completion: float = 0.5) -> dict[str, Any]:
    return answer({"verdict": "continue"}, completion=completion)


def abort(completed: list[str] | None = None, not_completed: list[str] | None = None) -> dict[str, Any]:
    return answer({"verdict": "abort", "abort": {"completed": completed or [], "not_completed": not_completed or ["交付"]}})


class FakeReviewGateway:
    """假 Model Gateway：review 的回答依次取自 answers（dict 是结构化输出，str 是不合格的文本，异常就抛出）；
    answers 是函数时按请求作答（用来模拟“永远原地重试”这类对抗性的模型）。"""

    def __init__(self, answers: Any = None, *, vendor: str = "vendor_r", cost_micros: int = 30_000) -> None:
        self.answers = answers if callable(answers) else list(answers or [])
        self.vendor = vendor
        self.cost_micros = cost_micros
        self.requests: list[mg.GenerateRequest] = []

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.requests.append(request)
        if callable(self.answers):
            reply = self.answers(request)
        else:
            if not self.answers:
                raise AssertionError("脚本里没有更多的 Progress Check 回答")
            reply = self.answers.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        response = mg.GenerateResponse(
            finish_reason=mg.FINISH_REASON_STOP,
            model=mg.ModelChoice(model_profile_id=f"{self.vendor}/{request.call.tier}", tier=request.call.tier, vendor=self.vendor),
            usage=metering_pb2.Usage(cost_usd_micros=self.cost_micros),
        )
        if isinstance(reply, str):
            response.text = reply
        else:
            value = struct_pb2.Value()
            json_format.ParseDict(reply, value)
            response.result.CopyFrom(value)
        return response

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        raise AssertionError("Reviewer 不做嵌入")

    def prompt(self, index: int = -1) -> str:
        return "\n".join(m.text for m in self.requests[index].generation.messages)


def always(reply: dict[str, Any]) -> Any:
    """对抗性的模型：不管看到什么都给同一个回答。"""
    return lambda _request: reply
