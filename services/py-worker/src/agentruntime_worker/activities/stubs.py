"""S-03 的桩：固定计划、桩模型、永远判 done（计划 S-03 目标）。

固定计划由 Task 的 inputs.stub_plan 选择：
  - answer（默认）：一个 model 节点 answer（role = deliver）。
  - slow_answer：同上，但 answer 一直发心跳，直到 <gate_dir>/<task_id> 出现才返回；测试用它在 Stage 执行中
    杀 Worker、取消 Task、观察写 Task 串行。
  - human_gate：human 节点 confirm（role = gate，选项 approve、reject）之后才是 answer。
  - fail_model：answer 以不可重试的错误失败。
inputs.stub_rounds = N 时，前 N-1 个 Round 的结果标记为未完成，桩 Reviewer 判 continue。

桩 Reviewer 只看 Stage 结果：结果都标记完成时判 done，交付来自 role = deliver 的节点。
"""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentruntime_worker import names
from agentruntime_worker.activities.calllog import CallLog
from agentruntime_worker.activities.types import (
    FinalizeTaskInput,
    NodeInput,
    PlanRoundInput,
    PlanRoundOutput,
    ReviewRoundInput,
)
from agentruntime_worker.config import WorkerConfig

STUB_PLANS = ("answer", "slow_answer", "human_gate", "fail_model")
HUMAN_OPTIONS = ("approve", "reject")
_MODES = {"slow_answer": "slow", "fail_model": "fail"}
# 等门打开时轮询的间隔；心跳由 SDK 按 heartbeat 超时节流后再发给服务端。
_POLL_INTERVAL_S = 0.1


class StubActivities:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.calls = CallLog(config.call_log)

    @activity.defn(name=names.PLAN_ROUND)
    async def plan_round(self, inp: PlanRoundInput) -> PlanRoundOutput:
        self.calls.record(names.PLAN_ROUND, inp.task_id, "start", round_seq=inp.round_seq)
        plan = str(inp.inputs.get("stub_plan") or "answer")
        if plan not in STUB_PLANS:
            self.calls.record(names.PLAN_ROUND, inp.task_id, "error")
            raise ApplicationError(f"未知的桩计划 {plan}", type="unknown_stub_plan", non_retryable=True)
        rounds = int(inp.inputs.get("stub_rounds") or 1)
        final = inp.round_seq >= rounds
        answer: dict[str, Any] = {
            "id": "answer",
            "kind": "atomic",
            "template": "stub_answer@v1",
            "executor": "model",
            "output": "generate",
            "effect": "none",
            "role": "deliver",
            "inputs": {"prompt": inp.message, "final": final, "mode": _MODES.get(plan, "fast")},
            "budget": {"timeout_s": self.config.stub_node_timeout_s},
            "retry": {"max": 3},
        }
        nodes = [answer]
        if plan == "human_gate":
            confirm = {
                "id": "confirm",
                "kind": "atomic",
                "template": "stub_confirm@v1",
                "executor": "human",
                "effect": "none",
                "role": "gate",
                "inputs": {"question": "按这个范围继续吗？", "options": list(HUMAN_OPTIONS)},
            }
            answer["after"] = ["confirm"]
            nodes = [confirm, answer]
        stage = {
            "seq": inp.round_seq,
            "goal": "回答用户的问题" if final else f"第 {inp.round_seq} 轮：收集信息",
            "exit_when": "answer 节点给出回答",
            "nodes": nodes,
        }
        self.calls.record(names.PLAN_ROUND, inp.task_id, "end", round_seq=inp.round_seq)
        return PlanRoundOutput(stage=stage, triage_level="fast")

    @activity.defn(name=names.EXECUTE_MODEL_NODE)
    async def execute_model_node(self, inp: NodeInput) -> dict[str, Any]:
        """返回 Node Output（node_output.schema.json）。"""
        node = inp.node
        node_inputs = node.get("inputs", {})
        self.calls.record(names.EXECUTE_MODEL_NODE, inp.task_id, "start", node_id=node["id"])
        try:
            mode = node_inputs.get("mode", "fast")
            if mode == "fail":
                self.calls.record(names.EXECUTE_MODEL_NODE, inp.task_id, "error", node_id=node["id"])
                raise ApplicationError("桩模型按计划失败", type="stub_failure", non_retryable=True)
            if mode == "slow":
                await self._wait_for_gate(inp.task_id)
        except asyncio.CancelledError:
            self.calls.record(names.EXECUTE_MODEL_NODE, inp.task_id, "cancelled", node_id=node["id"])
            raise
        output = {
            "result": {"text": f"桩回答：{node_inputs.get('prompt', '')}", "final": bool(node_inputs.get("final", True))},
            "rationale": "桩模型直接复述问题。",
            "meta": {"tier": "stub@low", "tokens": {"input": 0, "output": 0}, "cost_usd": 0, "latency_ms": 0},
        }
        self.calls.record(names.EXECUTE_MODEL_NODE, inp.task_id, "end", node_id=node["id"])
        return output

    async def _wait_for_gate(self, task_id: str) -> None:
        if self.config.gate_dir is None:
            return
        gate = self.config.gate_dir / task_id
        first = True
        while not gate.exists():
            activity.heartbeat()
            if first:
                self.calls.record(names.EXECUTE_MODEL_NODE, task_id, "heartbeat")
                first = False
            await asyncio.sleep(_POLL_INTERVAL_S)

    @activity.defn(name=names.EXECUTE_PROGRAM_NODE)
    async def execute_program_node(self, inp: NodeInput) -> dict[str, Any]:
        """program 节点的桩，S-05 替换：原样返回节点的常量输入。"""
        self.calls.record(names.EXECUTE_PROGRAM_NODE, inp.task_id, "start", node_id=inp.node["id"])
        output = {"result": dict(inp.node.get("inputs", {}))}
        self.calls.record(names.EXECUTE_PROGRAM_NODE, inp.task_id, "end", node_id=inp.node["id"])
        return output

    @activity.defn(name=names.REVIEW_ROUND)
    async def review_round(self, inp: ReviewRoundInput) -> dict[str, Any]:
        """返回 Verdict（verdict.schema.json）。"""
        self.calls.record(names.REVIEW_ROUND, inp.task_id, "start", round_seq=inp.round_seq)
        results = inp.stage_results
        not_final = any(isinstance(r.result, dict) and r.result.get("final") is False for r in results.values())
        delivers = sorted(node_id for node_id, r in results.items() if r.role == "deliver" and r.status == "succeeded")
        verdict: dict[str, Any]
        if not_final:
            verdict = {
                "verdict": "continue",
                "reason": "本轮结果标记为未完成，进入下一个 Round",
                "progress_check": {"criteria_met": False, "progress": True, "looping": False, "assumptions_hold": True, "next_step": "Planner 规划下一轮"},
            }
        elif delivers:
            verdict = {
                "verdict": "done",
                "reason": "桩 Reviewer：结果齐全",
                "done": {"deliver_node": delivers[0]},
                "progress_check": {"criteria_met": True, "progress": True, "looping": False, "assumptions_hold": True, "next_step": "交付"},
            }
        else:
            verdict = {"verdict": "abort", "reason": "没有可交付的 deliver 节点", "abort": {"completed": [], "not_completed": ["交付"]}}
        self.calls.record(names.REVIEW_ROUND, inp.task_id, "end", round_seq=inp.round_seq, verdict=verdict["verdict"])
        return verdict

    @activity.defn(name=names.FINALIZE_TASK)
    async def finalize_task(self, inp: FinalizeTaskInput) -> None:
        """收尾写回（附录 B.9）；S-03 只记调用。"""
        self.calls.record(names.FINALIZE_TASK, inp.task_id, "start", status=inp.status)
        self.calls.record(names.FINALIZE_TASK, inp.task_id, "end", status=inp.status)

    def activities(self) -> dict[str, Any]:
        """activity 名 → 绑定的方法。"""
        return {
            names.PLAN_ROUND: self.plan_round,
            names.EXECUTE_MODEL_NODE: self.execute_model_node,
            names.EXECUTE_PROGRAM_NODE: self.execute_program_node,
            names.REVIEW_ROUND: self.review_round,
            names.FINALIZE_TASK: self.finalize_task,
        }
