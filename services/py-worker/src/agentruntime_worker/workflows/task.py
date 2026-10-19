"""Task 工作流（设计书 3.2.1、附录 K.1）：Round 循环 Planner activity → Stage 执行 → Reviewer activity。

- Task State 是工作流状态，经 task_state 查询读取（附录 B.1）。
- 取消：传到正在运行的 activity，human 节点的等待随之结束；收尾写回是不可取消的清理步骤。
- 结束时（done、abort、取消、失败）先收尾写回，再给 Session 工作流发 task_finished。
  Session 工作流 Continue-As-New 之后拿不到子工作流的 future，只能靠这个信号得知结束。
- 版本清单由 Task Service 写入工作流输入，原样交给 Planner 并写进 Task State 的 pinned。

Stage 由 Stage Runner 执行（S-04，workflows/stage_runner.py）。S-03 的旧执行路径保留在版本分支后面，
S-03 留下的事件历史仍按旧路径回放。

工作流代码必须确定：不读系统时间、不用 random、不做 I/O、不调用模型（CLAUDE.md）。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, CancelledError, FailureError

with workflow.unsafe.imports_passed_through():
    from google.protobuf import json_format, struct_pb2

    from agentruntime.common.v1 import enums_pb2, error_pb2, payload_pb2
    from agentruntime.workflow.v1 import task_pb2

    from agentruntime_worker import names
    from agentruntime_worker.activities.types import (
        FinalizeTaskInput,
        NodeInput,
        PlanRoundInput,
        PlanRoundOutput,
        ReviewRoundInput,
        StagePolicy,
        StageResult,
        Tenant,
    )
    from agentruntime_worker.workflows import timeouts
    from agentruntime_worker.workflows.failures import caused_by_cancel as _caused_by_cancel
    from agentruntime_worker.workflows.failures import failure_message as _failure_message
    from agentruntime_worker.workflows.stage_runner import RunContext, StageRunner, effect_name

# Round 上限；Reviewer 的升级规则（附录 B.8）由 S-10 实现，这里只防止无限循环。
MAX_ROUNDS = 20
# Stage Runner（S-04）改变了命令序列：新的运行走这个版本分支，S-03 留下的历史仍按旧路径回放。
STAGE_RUNNER_PATCH = "s04-stage-runner"
DEFAULT_DONE_WHEN = "回答用户的问题"

DECISION_RETRY = timeouts.DECISION_RETRY


def _enum_name(value: int, prefix: str, enum: Any) -> str:
    return enum.Name(value).removeprefix(prefix).lower()


def _status_name(status: int) -> str:
    return _enum_name(status, "TASK_STATUS_", enums_pb2.TaskStatus)


def _to_value(obj: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(obj, value)
    return value


def _initial_state(inp: task_pb2.TaskWorkflowInput) -> dict[str, Any]:
    inputs = json_format.MessageToDict(inp.inputs)
    pins = inp.pinned_versions
    pinned: dict[str, Any] = {"recipe": pins.recipe or None}
    for key in ("node_templates", "reasoning_templates", "model_profiles"):
        entries = getattr(pins, key)
        if entries:
            pinned[key] = {k: entries[k] for k in sorted(entries)}
    # 领域约束（例如 quarter）来自用户的结构化输入，节点用 $task.constraints.<名> 引用（附录 B.1）；
    # effect 上限与预算以租户策略为准，用户输入不能覆盖。
    constraints: dict[str, Any] = dict(inputs.get("constraints") or {}) if isinstance(inputs.get("constraints"), dict) else {}
    if inp.effect_ceiling:
        constraints["effect_ceiling"] = _enum_name(inp.effect_ceiling, "EFFECT_", enums_pb2.Effect)
    if inp.budget.usd_micros:
        constraints["budget_usd"] = inp.budget.usd_micros / 1_000_000
    state: dict[str, Any] = {
        "task_id": inp.task_id,
        "session_id": inp.tenant.session_id,
        "goal": inp.message,
        "done_when": str(inputs.get("done_when") or DEFAULT_DONE_WHEN),
        "status": "running",
        "milestones": [],
        "facts": [],
        "assumptions": [],
        "open_questions": [],
        "rounds": [],
        "artifacts": [],
        "pinned": pinned,
    }
    if constraints:
        state["constraints"] = constraints
    return state


def _topological(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按依赖排序；同时可跑的按 Node id 排序，顺序确定。"""
    remaining = {n["id"]: n for n in nodes}
    done: list[str] = []
    order: list[dict[str, Any]] = []
    while remaining:
        ready = sorted(nid for nid, n in remaining.items() if all(dep in done for dep in n.get("after", [])))
        if not ready:
            raise ApplicationError("Stage 的依赖有环或引用了不存在的 Node", type="invalid_stage", non_retryable=True)
        for nid in ready:
            order.append(remaining.pop(nid))
            done.append(nid)
    return order


@workflow.defn(name=names.TASK_WORKFLOW)
class TaskWorkflow:
    def __init__(self) -> None:
        self._input: task_pb2.TaskWorkflowInput | None = None
        self._tenant = Tenant(tenant_id="")
        self._state: dict[str, Any] = {}
        self._status: int = enums_pb2.TASK_STATUS_QUEUED
        self._round_seq = 0
        # 正在等人工回复的节点 → 可选项；收到的合法回复。
        self._waiting: dict[str, tuple[str, ...]] = {}
        self._replies: dict[str, task_pb2.HumanReplySignal] = {}
        # Stage Runner 的上限：第一个 Stage 开始时加载，整个 Task 内沿用。
        self._stage_policy: StagePolicy | None = None
        self._pinned: dict[str, Any] = {}

    @workflow.run
    async def run(self, inp: task_pb2.TaskWorkflowInput) -> task_pb2.TaskWorkflowResult:
        self._input = inp
        self._tenant = Tenant(
            tenant_id=inp.tenant.tenant_id,
            user_id=inp.tenant.user_id,
            workspace_id=inp.tenant.workspace_id,
            session_id=inp.tenant.session_id,
        )
        self._state = _initial_state(inp)
        self._set_status(enums_pb2.TASK_STATUS_RUNNING)
        try:
            result = await self._loop()
        except (asyncio.CancelledError, CancelledError):
            await self._cancelled()
            raise
        except (FailureError, asyncio.TimeoutError) as err:
            if _caused_by_cancel(err):
                await self._cancelled()
                raise asyncio.CancelledError() from err
            workflow.logger.warning("Task 失败：%s", _failure_message(err))
            self._set_status(enums_pb2.TASK_STATUS_FAILED)
            result = self._result(error=error_pb2.Error(code="task_failed", message=_failure_message(err)))
        await self._wrap_up(result)
        return result

    @workflow.signal(name=names.HUMAN_REPLY_SIGNAL)
    def human_reply(self, reply: task_pb2.HumanReplySignal) -> None:
        options = self._waiting.get(reply.question_id)
        if options is None or (options and reply.option not in options):
            workflow.logger.warning("忽略不匹配的人工回复：%s", reply.question_id)
            return
        self._replies[reply.question_id] = reply

    @workflow.query(name=names.TASK_STATE_QUERY)
    def task_state(self) -> task_pb2.TaskStateQueryResult:
        result = task_pb2.TaskStateQueryResult(
            task_id=self._input.task_id if self._input else "",
            status=self._status,
            round_seq=self._round_seq,
        )
        if self._state:
            result.task_state.CopyFrom(payload_pb2.JsonPayload(schema_id=names.TASK_STATE_SCHEMA_ID, value=_to_value(self._state)))
        return result

    # --- Round 循环 ---

    async def _loop(self) -> task_pb2.TaskWorkflowResult:
        assert self._input is not None
        inp = self._input
        inputs = json_format.MessageToDict(inp.inputs)
        pinned = {
            "recipe": inp.pinned_versions.recipe,
            "node_templates": dict(sorted(inp.pinned_versions.node_templates.items())),
            "reasoning_templates": dict(sorted(inp.pinned_versions.reasoning_templates.items())),
            "model_profiles": dict(sorted(inp.pinned_versions.model_profiles.items())),
            "tenant_policy": inp.pinned_versions.tenant_policy,
        }
        self._pinned = pinned
        for seq in range(1, MAX_ROUNDS + 1):
            self._round_seq = seq
            plan = await workflow.execute_activity(
                names.PLAN_ROUND,
                PlanRoundInput(
                    tenant=self._tenant,
                    task_id=inp.task_id,
                    round_seq=seq,
                    task_state=self._state,
                    message=inp.message,
                    inputs=inputs,
                    attachments=list(inp.attachments),
                    pinned=pinned,
                ),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=DECISION_RETRY,
                result_type=PlanRoundOutput,
            )
            stage = plan.stage
            round_entry: dict[str, Any] = {"seq": seq, "stage_goal": stage["goal"], "triage_level": plan.triage_level, "status": "running"}
            self._state["rounds"].append(round_entry)

            results = await self._run_stage(seq, stage)

            round_entry["status"] = "reviewing"
            verdict = await workflow.execute_activity(
                names.REVIEW_ROUND,
                ReviewRoundInput(
                    tenant=self._tenant,
                    task_id=inp.task_id,
                    round_seq=seq,
                    task_state=self._state,
                    stage_results=results,
                    done_when=self._state["done_when"],
                ),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=DECISION_RETRY,
                result_type=dict,
            )
            kind = verdict["verdict"]
            round_entry["status"] = "completed"
            round_entry["verdict"] = kind
            if kind == "done":
                deliver = verdict["done"]["deliver_node"]
                self._set_status(enums_pb2.TASK_STATUS_DONE)
                return self._result(verdict=enums_pb2.VERDICT_DONE, deliver_node=deliver, deliverable=results[deliver].result)
            if kind == "abort":
                self._set_status(enums_pb2.TASK_STATUS_ABORTED)
                return self._result(verdict=enums_pb2.VERDICT_ABORT)
            if kind not in ("continue", "replan"):
                raise ApplicationError(f"S-03 还不处理 Verdict {kind}", type="unsupported_verdict", non_retryable=True)
        raise ApplicationError(f"超过 Round 上限 {MAX_ROUNDS}", type="too_many_rounds", non_retryable=True)

    async def _run_stage(self, seq: int, stage: dict[str, Any]) -> dict[str, StageResult]:
        """执行一个 Stage。新的运行交给 Stage Runner；S-03 留下的历史没有版本标记，按旧路径回放。"""
        assert self._input is not None
        if not workflow.patched(STAGE_RUNNER_PATCH):
            return await self._run_stage_v1(seq, stage)
        ctx = RunContext(
            tenant=self._tenant,
            task_id=self._input.task_id,
            round_seq=seq,
            effect_ceiling=effect_name(self._input.effect_ceiling),
            task_state=self._state,
            pinned=self._pinned,
            depth=self._input.depth,
            policy=self._stage_policy,
            task_input=self._input,
        )
        results = await StageRunner(ctx, self).run(stage)
        self._stage_policy = ctx.policy
        return results

    async def wait_for_reply(self, question_id: str, options: tuple[str, ...]) -> task_pb2.HumanReplySignal:
        """HumanGateway：等 human_reply 信号；只接受 options 里的选项；取消时等待随之结束。"""
        self._waiting[question_id] = tuple(options)
        try:
            await workflow.wait_condition(lambda: question_id in self._replies)
        finally:
            self._waiting.pop(question_id, None)
        return self._replies.pop(question_id)

    async def _run_stage_v1(self, seq: int, stage: dict[str, Any]) -> dict[str, StageResult]:
        """S-03 的执行路径：按依赖顺序逐个执行 Node。只为回放 S-03 留下的历史保留。"""
        assert self._input is not None
        results: dict[str, StageResult] = {}
        for node in _topological(stage["nodes"]):
            executor = node.get("executor")
            if executor == "human":
                result = await self._wait_for_human(node)
            elif executor in ("model", "program"):
                start_to_close, heartbeat = timeouts.model_node_timeouts(node.get("budget", {}).get("timeout_s"))
                output = await workflow.execute_activity(
                    names.EXECUTE_MODEL_NODE if executor == "model" else names.EXECUTE_PROGRAM_NODE,
                    NodeInput(tenant=self._tenant, task_id=self._input.task_id, round_seq=seq, node=node),
                    task_queue=names.MODEL_QUEUE if executor == "model" else names.PROGRAM_QUEUE,
                    start_to_close_timeout=start_to_close,
                    heartbeat_timeout=heartbeat,
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1),
                        maximum_interval=timedelta(seconds=30),
                        maximum_attempts=int(node.get("retry", {}).get("max", 0)) + 1,
                    ),
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                    result_type=dict,
                )
                result = output.get("result")
            else:
                raise ApplicationError(f"不认识的 executor {executor}", type="invalid_stage", non_retryable=True)
            results[node["id"]] = StageResult(role=node["role"], status="succeeded", result=result)
        return results

    async def _wait_for_human(self, node: dict[str, Any]) -> dict[str, Any]:
        """human 节点：等 human_reply 信号；取消时等待随之结束。"""
        question_id = node["id"]
        self._waiting[question_id] = tuple(node.get("inputs", {}).get("options", ()))
        try:
            await workflow.wait_condition(lambda: question_id in self._replies)
        finally:
            self._waiting.pop(question_id, None)
        reply = self._replies.pop(question_id)
        return {"option": reply.option, "guidance": reply.guidance, "replied_by": reply.replied_by_user_id}

    # --- 结局与收尾 ---

    def _set_status(self, status: int) -> None:
        self._status = status
        if self._state:
            self._state["status"] = _status_name(status)

    def _result(
        self,
        *,
        verdict: int = enums_pb2.VERDICT_UNSPECIFIED,
        deliver_node: str = "",
        deliverable: Any = None,
        error: error_pb2.Error | None = None,
    ) -> task_pb2.TaskWorkflowResult:
        assert self._input is not None
        result = task_pb2.TaskWorkflowResult(
            task_id=self._input.task_id,
            status=self._status,
            final_verdict=verdict,
            deliver_node_id=deliver_node,
            rounds=len(self._state.get("rounds", [])),
        )
        if deliverable is not None:
            result.deliverable.CopyFrom(payload_pb2.JsonPayload(value=_to_value(deliverable)))
        if error is not None:
            result.error.CopyFrom(error)
        return result

    async def _cancelled(self) -> None:
        self._set_status(enums_pb2.TASK_STATUS_CANCELLED)
        # 收尾不可取消：即使再次收到取消，也要做完写回与通知。
        await asyncio.shield(asyncio.ensure_future(self._wrap_up(self._result())))

    async def _wrap_up(self, result: task_pb2.TaskWorkflowResult) -> None:
        """收尾：先写回，再通知 Session 工作流。两行调换会改变命令顺序，重放测试据此证明自己有效。"""
        await self._finalize(result)  # replay-mutation:A
        await self._notify_session(result)  # replay-mutation:B

    async def _finalize(self, result: task_pb2.TaskWorkflowResult) -> None:
        assert self._input is not None
        try:
            await workflow.execute_activity(
                names.FINALIZE_TASK,
                FinalizeTaskInput(
                    tenant=self._tenant,
                    task_id=self._input.task_id,
                    status=_status_name(result.status),
                    task_state=self._state,
                    deliver_node=result.deliver_node_id,
                ),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.FINALIZE_START_TO_CLOSE,
                retry_policy=DECISION_RETRY,
            )
        except FailureError as err:
            # 写回失败不改变 Task 的结局；经验与 trace 的写回是异步的，可以补做（附录 B.9）。
            workflow.logger.warning("收尾写回失败：%s", _failure_message(err))

    async def _notify_session(self, result: task_pb2.TaskWorkflowResult) -> None:
        assert self._input is not None
        parent = workflow.info().parent
        # sub_task 的父工作流是 Task，它从子工作流的 future 拿结果，不需要这个信号。
        if parent is None or self._input.parent_task_id:
            return
        try:
            await workflow.get_external_workflow_handle(parent.workflow_id).signal(names.TASK_FINISHED_SIGNAL, result)
        except FailureError as err:
            workflow.logger.warning("通知 Session 工作流失败：%s", _failure_message(err))
