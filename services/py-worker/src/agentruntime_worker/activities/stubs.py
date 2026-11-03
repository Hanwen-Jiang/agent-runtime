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
import json
from typing import Any

from google.protobuf import json_format, struct_pb2
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import InvalidError

from agentruntime_worker import names
from agentruntime_worker.activities.calllog import CallLog
from agentruntime_worker.activities.types import (
    CatalogInfo,
    CatalogRequest,
    FinalizeTaskInput,
    HookCall,
    PlanRoundInput,
    PlanRoundOutput,
    ReviewRoundInput,
    StagePolicy,
    StagePolicyRequest,
    TemplateExpansion,
    TemplateExpansionRequest,
    TemplateInfo,
    ToolCallInput,
    ToolLoopStep,
    ToolLoopStepInput,
)
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.tool_gate import require_tool_gate

STUB_PLANS = ("answer", "slow_answer", "human_gate", "fail_model", "deliver_file")
HUMAN_OPTIONS = ("approve", "reject")
_MODES = {"slow_answer": "slow", "fail_model": "fail"}
# 等门打开时轮询的间隔；心跳由 SDK 按 heartbeat 超时节流后再发给服务端。
_POLL_INTERVAL_S = 0.1


class StubModelGateway:
    """S-03 的桩模型，作为 Model Gateway 的开发实现（executor.model_gateway.ModelGateway）。

    按 Node 输入里的 mode 行事：fast 立即返回；slow 等 <gate_dir>/<task_id> 出现；fail 抛不可重试的错误。
    结果是 {"text", "final"}，另带 rationale。调用记录（activity 名 execute_model_node）与 S-03 相同，端到端测试据此断言。
    """

    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.calls = CallLog(config.call_log)

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        task_id, node_id = request.call.task_id, request.call.node_id
        user = next((m.text for m in request.generation.messages if m.role == mg.CHAT_ROLE_USER), "{}")
        inputs = (json.loads(user).get("inputs") or {}) if user.startswith("{") else {}
        self.calls.record(names.EXECUTE_MODEL_NODE, task_id, "start", node_id=node_id)
        try:
            mode = inputs.get("mode", "fast")
            if mode == "fail":
                self.calls.record(names.EXECUTE_MODEL_NODE, task_id, "error", node_id=node_id)
                raise InvalidError("stub_failure", "桩模型按计划失败")
            if mode == "slow":
                await self._wait_for_gate(task_id)
        except asyncio.CancelledError:
            self.calls.record(names.EXECUTE_MODEL_NODE, task_id, "cancelled", node_id=node_id)
            raise
        result = struct_pb2.Value()
        json_format.ParseDict({"text": f"桩回答：{inputs.get('prompt', '')}", "final": bool(inputs.get("final", True))}, result)
        self.calls.record(names.EXECUTE_MODEL_NODE, task_id, "end", node_id=node_id)
        return mg.GenerateResponse(
            result=result,
            rationale="桩模型直接复述问题。",
            finish_reason=mg.FINISH_REASON_STOP,
            model=mg.ModelChoice(model_profile_id="stub/model", tier="stub@low", vendor="stub"),
        )

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        return mg.EmbedResponse(embeddings=[mg.Embedding(values=[0.0, 0.0, 0.0]) for _ in request.texts], embedding_version="stub-v1")

    async def _wait_for_gate(self, task_id: str) -> None:
        """等 <gate_dir>/<task_id> 出现；心跳由 Node Executor 在后台发。"""
        if self.config.gate_dir is None:
            return
        gate = self.config.gate_dir / task_id
        first = True
        while not gate.exists():
            if first:
                self.calls.record(names.EXECUTE_MODEL_NODE, task_id, "heartbeat")
                first = False
            await asyncio.sleep(_POLL_INTERVAL_S)


def stub_tool_effects(tools: list[str]) -> dict[str, str]:
    """桩工具目录：工具一律按 read。只有 StubActivities 用（测试）；Worker 的 resolve_catalog 取自 ToolDriver 的登记（DevCatalog，T-10）。"""
    return {t: "read" for t in tools}


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
        if plan == "deliver_file":
            # N-18（F3）的端到端：一个 tool_loop 先调 deliver_file 交付工作区里的文件（inputs.stub_file），再由 answer 交付文字。
            path = str(inp.inputs.get("stub_file") or "out/report.txt")
            deliver = {
                "id": "deliver_file",
                "kind": "goal",
                "mode": "tool_loop",
                "goal": "把工作区里的文件交付给用户",
                "done_when": "文件已交付",
                "output_schema": "result_v1",
                "tools": ["deliver_file"],
                "effect_ceiling": "write_reversible",
                "inputs": {"stub_tool_calls": [{"tool": "deliver_file", "args": {"path": path}}]},
            }
            answer["after"] = ["deliver_file"]
            nodes = [deliver, answer]
        stage = {
            "seq": inp.round_seq,
            "goal": "回答用户的问题" if final else f"第 {inp.round_seq} 轮：收集信息",
            "exit_when": "answer 节点给出回答",
            "nodes": nodes,
        }
        self.calls.record(names.PLAN_ROUND, inp.task_id, "end", round_seq=inp.round_seq)
        return PlanRoundOutput(stage=stage, triage_level="fast")

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

    # --- Stage Runner 的桩（S-04）；真实实现分别来自租户策略、Registry、Model Gateway、ToolDriver 与 Policy Engine ---

    @activity.defn(name=names.LOAD_STAGE_POLICY)
    async def load_stage_policy(self, inp: StagePolicyRequest) -> StagePolicy:
        """默认上限。"""
        return StagePolicy()

    @activity.defn(name=names.RESOLVE_CATALOG)
    async def resolve_catalog(self, req: CatalogRequest) -> CatalogInfo:
        """桩 Catalog：模板一律按 program · none，工具一律按 read。"""
        return CatalogInfo(
            templates={t: TemplateInfo(executor="program", effect="none") for t in req.templates},
            tools=stub_tool_effects(list(req.tools)),
        )

    @activity.defn(name=names.EXPAND_TEMPLATE)
    async def expand_template(self, req: TemplateExpansionRequest) -> TemplateExpansion:
        """桩 Registry 没有 Recipe：一律报不可重试的错误。"""
        raise ApplicationError(f"桩 Registry 里没有 Recipe {req.node.get('recipe')}", type="recipe_not_found", non_retryable=True)

    @activity.defn(name=names.TOOL_LOOP_STEP)
    async def tool_loop_step(self, inp: ToolLoopStepInput) -> ToolLoopStep:
        """桩模型：第一步就结束，结果为空。Goal Node 的 inputs 写了 stub_tool_calls 时，第一步照它调工具（一次可以几个），
        第二步结束并带回各次的观察（端到端测试用，例如 deliver_file 桩计划）。"""
        calls = [c for c in (inp.node.get("inputs") or {}).get("stub_tool_calls") or [] if isinstance(c, dict) and c.get("tool")]
        if calls and not inp.history:
            return ToolLoopStep(action="tool", tool=str(calls[0]["tool"]), args=dict(calls[0].get("args") or {}), calls=calls)
        if calls:
            return ToolLoopStep(action="finish", result={"observations": [h.get("observation", {"error": h.get("error")}) for h in inp.history]})
        return ToolLoopStep(action="finish", result={})

    @activity.defn(name=names.CALL_TOOL)
    async def call_tool(self, inp: ToolCallInput) -> Any:
        """桩工具：回显参数。不可逆工具先查审批记录（D16 兜底）。不在 activities() 里：Worker 的 call_tool 是 Node Executor 的（T-10）。"""
        require_tool_gate(inp)
        return {"tool": inp.tool, "args": inp.args}

    @activity.defn(name=names.RUN_HOOKS)
    async def run_hooks(self, call: HookCall) -> list[dict[str, Any]]:
        """Policy Engine 的桩：一律 allow（计划 S-04 交付）。"""
        return [{"decision": "allow", "hook": hook, "mount": call.mount} for hook in call.hooks]

    def activities(self) -> dict[str, Any]:
        """activity 名 → 绑定的方法。"""
        return {
            names.PLAN_ROUND: self.plan_round,
            names.REVIEW_ROUND: self.review_round,
            names.FINALIZE_TASK: self.finalize_task,
            names.LOAD_STAGE_POLICY: self.load_stage_policy,
            names.RESOLVE_CATALOG: self.resolve_catalog,
            names.EXPAND_TEMPLATE: self.expand_template,
            names.TOOL_LOOP_STEP: self.tool_loop_step,
            names.RUN_HOOKS: self.run_hooks,
        }
