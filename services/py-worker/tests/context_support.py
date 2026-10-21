"""Context Builder 测试共用：按用途作答的假 Model Gateway、轨迹构造、Task State 与 Task Plan。

假 Gateway 按 CallContext.node_id 的后缀分派：#summary 是增量摘要，#probe 是 Compaction Probe，其余是 tool_loop 的一步。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.context.builder import ContextConfig, ContextRequest

TASK_ID = "tsk_01J" + "0" * 22 + "C"
SESSION_ID = "ses_01J" + "0" * 22 + "C"
TENANT = Tenant(tenant_id="t1", user_id="u1", session_id=SESSION_ID)
OTHER_TENANT = Tenant(tenant_id="t2", user_id="u2", session_id=SESSION_ID)
SYSTEM = "你是 tool_loop 的执行者：按目标调用工具，完成后给出结果。"

TASK_STATE: dict[str, Any] = {
    "task_id": TASK_ID,
    "session_id": SESSION_ID,
    "goal": "修复 CI 里失败的 test_checkout",
    "done_when": "make test 全部通过，且 PR #482 的检查变绿",
    "constraints": {"effect_ceiling": "write_reversible", "budget_usd": 2.5},
    "milestones": ["复现失败", "定位原因", "提交修复"],
    "facts": ["测试依赖本地 Redis 的 6379 端口", "失败只出现在 Python 3.13 的矩阵里"],
    "assumptions": ["可能是时区相关的断言"],
    "open_questions": ["fixtures/orders.json 是否需要更新"],
    "artifacts": ["obj://t1/artifacts/patch-checkout.diff"],
}
TASK_PLAN: dict[str, Any] = {
    "triage": {"level": "full", "task_family": "engineering.ci_fix", "confidence": 0.8},
    "horizon": "per_stage",
    "context": {"work_mode": "coding", "must_keep": ["失败测试的名字"]},
}
GOAL_NODE: dict[str, Any] = {
    "id": "fix",
    "kind": "goal",
    "goal": "修复失败的 test_checkout",
    "done_when": "make test 通过",
    "mode": "tool_loop",
    "tools": ["repo_read", "run_tests", "repo_edit", "web_search"],
    "output_schema": {"type": "object"},
}


def objects(root: Path) -> LocalDiskObjectStoreProvider:
    return LocalDiskObjectStoreProvider(root / "objects", signing_key=b"context-tests")


def filler(i: int, chars: int) -> str:
    """第 i 步的观察正文：可辨认的开头加填充，总长 chars。"""
    head = f"<<step {i} body>> "
    return head + ("lorem ipsum dolor sit amet " * (chars // 27 + 1))[: max(chars - len(head), 0)]


def read_step(i: int, chars: int = 800) -> dict[str, Any]:
    return {"tool": "repo_read", "args": {"path": f"src/module_{i}.py"}, "observation": {"path": f"src/module_{i}.py", "exit_code": 0, "content": filler(i, chars)}}


def run_tests_step(i: int, chars: int = 800, *, failed: tuple[str, ...] = ("test_checkout",)) -> dict[str, Any]:
    return {"tool": "run_tests", "args": {"target": "make test"}, "observation": {"exit_code": 1, "failed": list(failed), "stdout": filler(i, chars)}}


def search_step(i: int, chars: int = 800) -> dict[str, Any]:
    return {
        "tool": "web_search",
        "args": {"q": f"query {i}"},
        "observation": {"url": f"https://docs.example/page-{i}", "title": f"Page {i}", "text": filler(i, chars)},
    }


def error_step(i: int) -> dict[str, Any]:
    return {"tool": "repo_edit", "args": {"path": f"src/module_{i}.py"}, "error": f"patch 第 {i} 处不适用：上下文不匹配"}


def history(n: int, chars: int = 800) -> list[dict[str, Any]]:
    """n 步编码轨迹：读文件与跑测试交替。"""
    return [run_tests_step(i, chars) if i % 3 == 2 else read_step(i, chars) for i in range(n)]


def request(steps: list[dict[str, Any]], *, task_plan: dict[str, Any] | None = None, round_seq: int = 1, tenant: Tenant = TENANT, **extra: Any) -> ContextRequest:
    return ContextRequest(
        tenant=tenant,
        task_id=TASK_ID,
        round_seq=round_seq,
        node=GOAL_NODE,
        history=steps,
        task_state=TASK_STATE,
        task_plan=TASK_PLAN if task_plan is None else task_plan,
        system=[SYSTEM],
        **extra,
    )


def config(effective_tokens: int = 4_000, **extra: Any) -> ContextConfig:
    return ContextConfig(effective_tokens=effective_tokens, **extra)


def text_of(messages: list[mg.ChatMessage]) -> str:
    return "\n".join(m.text + "".join(json_format.MessageToJson(c.arguments) for c in m.tool_calls) for m in messages)


def delta(**sections: list[str]) -> dict[str, list[str]]:
    return {s: sections.get(s, []) for s in ("intent", "decisions", "artifacts", "open", "next")}


Answer = dict[str, Any] | str | BaseException | Callable[[mg.GenerateRequest], Any]


class ContextGateway:
    """假 Model Gateway。summaries、probes、steps 是依次返回的回答：dict 是结构化输出，str 是文本，异常就抛出，
    函数按请求给出回答。probe 没有脚本时按 lexical_answers 作答（在上下文里找题目对应的片段）。"""

    def __init__(self, *, summaries: list[Answer] | None = None, probes: list[Answer] | None = None, steps: list[Any] | None = None) -> None:
        self.summaries = list(summaries or [])
        self.probes = list(probes or [])
        self.steps = list(steps or [])
        self.requests: list[mg.GenerateRequest] = []

    def of(self, purpose: str) -> list[mg.GenerateRequest]:
        return [r for r in self.requests if purpose_of(r) == purpose]

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        self.requests.append(request)
        purpose = purpose_of(request)
        if purpose == "summary":
            answer: Any = self.summaries.pop(0) if self.summaries else delta(intent=["（未提供脚本）"])
        elif purpose == "probe":
            answer = self.probes.pop(0) if self.probes else lexical_answers
        else:
            answer = self.steps.pop(0)
        if callable(answer) and not isinstance(answer, BaseException):
            answer = answer(request)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, mg.GenerateResponse):
            return answer
        response = mg.GenerateResponse(finish_reason=mg.FINISH_REASON_STOP, rationale="模型的推理过程", model=mg.ModelChoice(tier=request.call.tier, vendor="mock"))
        if isinstance(answer, str):
            response.text = answer
        else:
            value = struct_pb2.Value()
            json_format.ParseDict(answer, value)
            response.result.CopyFrom(value)
        return response

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        return mg.EmbedResponse()


def purpose_of(request: mg.GenerateRequest) -> str:
    node_id = request.call.node_id
    return "summary" if node_id.endswith("#summary") else "probe" if node_id.endswith("#probe") else "step"


def questions_of(request: mg.GenerateRequest) -> list[str]:
    """Probe 请求里的题目：最后一条消息的 JSON {"questions": [...]}。"""
    return list(json.loads(request.generation.messages[-1].text)["questions"])


def lexical_answers(request: mg.GenerateRequest) -> dict[str, Any]:
    """按词面作答：对每道填空题，在上下文（除最后一条外的消息）里找与题面前后文相同的片段，取空白处对应的文字。"""
    context = text_of(list(request.generation.messages[:-1]))
    answers = []
    for q in questions_of(request):
        before, _, after = q.partition("____")
        before, after = before[-12:], after[:12]
        found = ""
        start = context.find(before) if before else -1
        while start >= 0:
            tail = context[start + len(before) :]
            end = tail.find(after) if after else min(len(tail), 40)
            if end > 0:
                found = tail[:end]
                break
            start = context.find(before, start + 1)
        answers.append(found)
    return {"answers": answers}


def wrong_answers(request: mg.GenerateRequest) -> dict[str, Any]:
    return {"answers": ["不知道" for _ in questions_of(request)]}


def tool_call(name: str, **args: Any) -> mg.GenerateResponse:
    arguments = struct_pb2.Struct()
    arguments.update(args)
    return mg.GenerateResponse(
        finish_reason=mg.FINISH_REASON_TOOL_CALLS,
        rationale="模型的推理过程",
        tool_calls=[mg.ToolCall(id="call_1", name=name, arguments=arguments)],
        model=mg.ModelChoice(tier="strong@medium", vendor="mock"),
    )
