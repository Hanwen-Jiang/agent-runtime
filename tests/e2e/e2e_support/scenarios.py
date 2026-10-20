"""故障注入用例共用的 Stage 片段与断言（计划 S-07）。

用例把 Stage 直接写进 Task 的 inputs.fault_stage，故障 Worker 的桩 Planner 原样交给 Stage Runner。
"""

from __future__ import annotations

import uuid
from typing import Any

from e2e_support.api import TaskApi, finished_payload, stream_until_finished
from e2e_support.stack import STUB_NODE_TIMEOUT_S, Stack, wait_until

# Planner 写的占位；Stage Runner 会按 tenant_id、task_id、node_id 与输入重新生成（附录 D.4）。
PLANNER_KEY = "planner-placeholder"


def unique(prefix: str) -> str:
    """用例独有的标记，放进 Node 输入，故障规则据此只影响本用例。"""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def write_node(node_id: str, tool: str, ref: str, *, irreversible: bool, retry_max: int = 3, echo_kb: int = 0) -> dict[str, Any]:
    inputs: dict[str, Any] = {"ref": ref, "amount": 10}
    if echo_kb:
        inputs["echo_kb"] = echo_kb
    node: dict[str, Any] = {
        "id": node_id,
        "kind": "atomic",
        "template": f"{tool}@v1",
        "executor": "program",
        "effect": "write_irreversible" if irreversible else "write_reversible",
        "role": "act",
        "inputs": inputs,
        "idempotency_key": PLANNER_KEY,
        "budget": {"timeout_s": STUB_NODE_TIMEOUT_S},
    }
    if not irreversible:
        node["retry"] = {"max": retry_max}
    return node


def answer_node(
    *,
    node_id: str = "answer",
    after: tuple[str, ...] = (),
    prompt: str = "汇报结果",
    mode: str = "fast",
    result_kb: int = 0,
    retry_max: int = 3,
    marker: str = "",
) -> dict[str, Any]:
    """deliver 节点（model，桩模型）。marker 放进输入，供故障规则匹配。"""
    inputs: dict[str, Any] = {"prompt": prompt, "final": True, "mode": mode}
    if result_kb:
        inputs["result_kb"] = result_kb
    if marker:
        inputs["marker"] = marker
    node: dict[str, Any] = {
        "id": node_id,
        "kind": "atomic",
        "template": "stub_answer@v1",
        "executor": "model",
        "output": "generate",
        "effect": "none",
        "role": "deliver",
        "inputs": inputs,
        "budget": {"timeout_s": STUB_NODE_TIMEOUT_S},
        "retry": {"max": retry_max},
    }
    if after:
        node["after"] = list(after)
    return node


def human_node(node_id: str = "confirm", *, timeout_s: int | None = None, options: tuple[str, ...] = ("approve", "reject")) -> dict[str, Any]:
    inputs: dict[str, Any] = {"question": "按这个范围继续吗？", "options": list(options)}
    if timeout_s is not None:
        inputs["timeout_s"] = timeout_s
    return {"id": node_id, "kind": "atomic", "template": "stub_confirm@v1", "executor": "human", "effect": "none", "role": "gate", "inputs": inputs}


def fault_stage(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"fault_stage": list(nodes)}


def outcome(api: TaskApi, sub: dict[str, Any], timeout: float = 180) -> dict[str, Any]:
    """读 SSE 直到 task_finished，返回 Task 的结局；再用 QueryTask 核对状态一致。"""
    result = finished_payload(stream_until_finished(sub["eventsUrl"], api.token or "", timeout=timeout))
    assert api.query(sub["taskId"])["status"] == result["status"], "SSE 的结局与 QueryTask 不一致"
    return result


def reviewer_saw(stack: Stack, task_id: str) -> dict[str, Any]:
    """桩 Reviewer 看到的各 Node 状态与错误（只有一个 Round）。"""
    saw = [c for c in stack.calls(task_id) if c["activity"] == "review_round" and c["event"] == "saw"]
    assert len(saw) == 1, f"Reviewer 应恰好判定一次，实际 {len(saw)} 次"
    return {"statuses": saw[0]["statuses"], "errors": saw[0]["errors"]}


def human_asks(stack: Stack, task_id: str, question_id: str) -> int:
    """经 HumanChannel 发出的问题数（发给人的通知是外部副作用）。"""
    return sum(1 for c in stack.calls(task_id) if c["activity"] == "human_channel" and c["event"] == "ask" and c["question_id"] == question_id)


def human_cancels(stack: Stack, task_id: str, question_id: str) -> int:
    return sum(1 for c in stack.calls(task_id) if c["activity"] == "human_channel" and c["event"] == "cancel" and c["question_id"] == question_id)


def wait_human_ask(stack: Stack, task_id: str, question_id: str, timeout: float = 60) -> None:
    wait_until(lambda: human_asks(stack, task_id, question_id) >= 1, f"{task_id} 发出问题 {question_id}", timeout)


def vendor_calls(stack: Stack, task_id: str, event: str = "start", vendor: str | None = None) -> list[dict[str, Any]]:
    return [c for c in stack.calls(task_id) if c["activity"] == "vendor_call" and c["event"] == event and (vendor is None or c["vendor"] == vendor)]
