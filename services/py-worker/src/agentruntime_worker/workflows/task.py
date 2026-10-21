"""Task 工作流（设计书 3.2.1、附录 K.1）：Round 循环 Planner activity → Stage 执行 → Reviewer activity。

- Task State 是工作流状态，经 task_state 查询读取（附录 B.1）。
- 取消：传到正在运行的 activity，human 节点的等待随之结束；收尾写回是不可取消的清理步骤。
- 结束时（done、abort、取消、失败）给 Session 工作流发 task_finished，并收尾写回。S-10 起先通知、再写回：
  写回全部异步，不阻塞交付，失败也不改变结局（附录 B.9）。Session 工作流 Continue-As-New 之后拿不到子工作流的
  future，只能靠这个信号得知结束。
- 版本清单由 Task Service 写入工作流输入，原样交给 Planner 并写进 Task State 的 pinned。
- Verdict 的去向（S-10，附录 B.4、B.8）：continue、replan、repair 换方法、rollback、reroute、问人后继续或重规划
  回 Planner；repair 原地重试与 escalate 换更强的 Tier 不回 Planner，重跑同一个 Stage 里出问题的部分；
  escalate 问人经 HumanChannel，回复限定三种；done、abort 与问人后停止收尾。Reviewer 的汇总写进 Task State。

Stage 由 Stage Runner 执行（S-04，workflows/stage_runner.py）。S-03 的旧执行路径保留在版本分支后面，
S-03 留下的事件历史仍按旧路径回放；S-10 之前的历史同样按旧的 Round 循环与收尾顺序回放。

工作流代码必须确定：不读系统时间、不用 random、不做 I/O、不调用模型（CLAUDE.md）。
"""

from __future__ import annotations

import asyncio
import dataclasses
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
        HumanCancel,
        HumanQuestion,
        NodeInput,
        PlanRoundInput,
        PlanRoundOutput,
        ReviewRoundInput,
        StagePolicy,
        StageResult,
        Tenant,
    )
    from agentruntime_worker.reviewer.rules import HUMAN_REPLIES
    from agentruntime_worker.reviewer.signals import history_entry
    from agentruntime_worker.reviewer.summary import apply_state_update
    from agentruntime_worker.workflows import timeouts
    from agentruntime_worker.workflows import verdicts as vd
    from agentruntime_worker.workflows.failures import caused_by_cancel as _caused_by_cancel
    from agentruntime_worker.workflows.failures import failure_message as _failure_message
    from agentruntime_worker.workflows.stage_runner import RunContext, StageRunner, effect_name

# Round 上限：兜底。Reviewer 的升级规则与各级次数上限（S-10）让一直失败的 Task 在 12 次评审之内收尾。
MAX_ROUNDS = 20
# Stage Runner（S-04）改变了命令序列：新的运行走这个版本分支，S-03 留下的历史仍按旧路径回放。
STAGE_RUNNER_PATCH = "s04-stage-runner"
# S-10：按 Verdict 的去向走（repair 重跑、问人、rollback、reroute……）与收尾先通知再写回，改变了命令序列。
REVIEWER_PATCH = "s10-reviewer"
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
        # S-10：是否走新的 Verdict 去向与收尾顺序（版本分支）；之前各 Round 的摘要；检查点（Round 序号 → Task State
        # 的快照）；各 Round 的 Stage 摘要；最近一个成功的 deliver 节点及其结果；结局的 Verdict；上一个 Stage 的成本。
        self._s10 = False
        self._history: list[dict[str, Any]] = []
        self._checkpoints: dict[int, dict[str, Any]] = {}
        self._stages: list[dict[str, Any]] = []
        self._delivery: tuple[str, Any] | None = None
        self._final_verdict: dict[str, Any] | None = None
        self._stage_cost = 0.0
        # 当前的 Task Plan：交给 Stage Runner，tool_loop 的 Context Builder 据此取工作模式与阈值（只是输入）。
        self._task_plan: dict[str, Any] | None = None

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
        # S-10 的 Round 循环改变了命令序列：新的运行走版本分支，之前的历史按下面的旧循环回放。
        self._s10 = workflow.patched(REVIEWER_PATCH)
        if self._s10:
            return await self._rounds(inputs, pinned)
        # 交给下一轮 Planner 的 Task Plan 与上一轮（S-09）：只改 activity 的输入，不改命令序列。
        task_plan: dict[str, Any] | None = None
        last_round: dict[str, Any] | None = None
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
                    task_plan=task_plan,
                    last_round=last_round,
                ),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=DECISION_RETRY,
                result_type=PlanRoundOutput,
            )
            stage = plan.stage
            round_entry: dict[str, Any] = {"seq": seq, "stage_goal": stage["goal"], "triage_level": plan.triage_level, "status": "running"}
            self._state["rounds"].append(round_entry)
            if plan.task_plan:
                self._task_plan = plan.task_plan

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
            if plan.task_plan:
                task_plan = plan.task_plan
            last_round = {"stage": stage, "results": {nid: dataclasses.asdict(r) for nid, r in results.items()}, "verdict": verdict}
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

    async def _rounds(self, inputs: dict[str, Any], pinned: dict[str, Any]) -> task_pb2.TaskWorkflowResult:
        """S-10 的 Round 循环：按 Verdict 的去向走（附录 B.4、B.8）。"""
        assert self._input is not None
        task_plan: dict[str, Any] | None = None
        last_round: dict[str, Any] | None = None
        rerun: vd.Rerun | None = None
        for seq in range(1, MAX_ROUNDS + 1):
            self._round_seq = seq
            carried: dict[str, StageResult] = {}
            injections: dict[str, list[dict[str, Any]]] = {}
            if rerun is None:
                plan = await self._plan(seq, inputs, pinned, task_plan, last_round)
                stage = plan.stage
                round_entry: dict[str, Any] = {"seq": seq, "stage_goal": stage["goal"], "triage_level": plan.triage_level, "status": "running"}
                if plan.task_plan:
                    task_plan = plan.task_plan
                    self._task_plan = task_plan
            else:
                # 原地重试或换更强的 Tier：不经 Planner，重跑同一个 Stage 里出问题的部分。
                stage, carried, injections = rerun.stage, rerun.carried, rerun.injections
                rerun = None
                round_entry = {"seq": seq, "stage_goal": stage["goal"], "status": "running"}
            self._state["rounds"].append(round_entry)

            results = await self._run_stage(seq, stage, carried=carried, injections=injections)
            round_entry["cost_usd"] = round(self._stage_cost, 6)
            self._stages.append(vd.stage_summary(stage))

            round_entry["status"] = "reviewing"
            verdict, update = await self._review(seq, stage, results)
            kind = verdict["verdict"]
            round_entry["status"] = "completed"
            round_entry["verdict"] = kind
            apply_state_update(self._state, update)
            as_dicts = {nid: dataclasses.asdict(r) for nid, r in results.items()}
            self._history.append(history_entry(seq, verdict, as_dicts))
            self._remember_delivery(results)
            last_round = {"stage": stage, "results": as_dicts, "verdict": verdict}

            if kind == "continue":
                self._checkpoints[seq] = vd.snapshot(self._state)
            elif kind == "done":
                return self._done(verdict, results)
            elif kind == "abort":
                return self._aborted(verdict)
            elif (kind == "repair" and verdict["repair"].get("strategy") == "retry_with_error") or (kind == "escalate" and verdict["escalate"].get("target") == "tier"):
                rerun = vd.rerun(stage, results, verdict, seq + 1)
            elif kind == "escalate":
                reply = await self._ask_human(seq, verdict)
                if reply is None:
                    return self._aborted(vd.stopped(results, f"问人没有得到回复：{verdict['reason']}；按停止处理，用现有结果收尾"))
                if reply.option == "stop_and_wrap_up":
                    return self._aborted(vd.stopped(results, f"人选择停止并用现有结果收尾：{verdict['reason']}"))
                last_round["human_reply"] = {"option": reply.option, "guidance": reply.guidance}
            elif kind == "rollback":
                round_no = vd.checkpoint_round(verdict)
                vd.restore(self._state, self._checkpoints.get(round_no) if round_no is not None else None)
            elif kind == "reroute":
                recipe = verdict["reroute"]["recipe"]
                self._state.setdefault("pinned", {})["recipe"] = recipe
                pinned["recipe"] = recipe
            # 其余（continue、replan、repair 换方法、rollback、reroute、问人后继续或重规划）：下一轮回 Planner。
        raise ApplicationError(f"超过 Round 上限 {MAX_ROUNDS}", type="too_many_rounds", non_retryable=True)

    async def _plan(
        self, seq: int, inputs: dict[str, Any], pinned: dict[str, Any], task_plan: dict[str, Any] | None, last_round: dict[str, Any] | None
    ) -> PlanRoundOutput:
        assert self._input is not None
        inp = self._input
        return await workflow.execute_activity(
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
                task_plan=task_plan,
                last_round=last_round,
            ),
            task_queue=names.DECISION_QUEUE,
            start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
            retry_policy=DECISION_RETRY,
            result_type=PlanRoundOutput,
        )

    async def _review(self, seq: int, stage: dict[str, Any], results: dict[str, StageResult]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Reviewer：输入只有 Stage 结果（含本轮执行的 Stage）、Task State、验收条件与之前各 Round 的摘要。"""
        assert self._input is not None
        raw = await workflow.execute_activity(
            names.REVIEW_ROUND,
            ReviewRoundInput(
                tenant=self._tenant,
                task_id=self._input.task_id,
                round_seq=seq,
                task_state=self._state,
                stage_results=results,
                done_when=self._state["done_when"],
                stage=stage,
                history=list(self._history),
            ),
            task_queue=names.DECISION_QUEUE,
            start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
            retry_policy=DECISION_RETRY,
            result_type=dict,
        )
        return vd.review_output(raw)

    def _remember_delivery(self, results: dict[str, StageResult]) -> None:
        for nid in sorted(results):
            r = results[nid]
            if r.role == "deliver" and r.status == "succeeded":
                self._delivery = (nid, r.result)

    def _done(self, verdict: dict[str, Any], results: dict[str, StageResult]) -> task_pb2.TaskWorkflowResult:
        deliver = str(verdict["done"].get("deliver_node", ""))
        r = results.get(deliver)
        if r is None or r.role != "deliver" or r.status != "succeeded":
            # 交付只来自本轮成功的 deliver 节点（附录 B.9）；判定不合法时让 Task 失败，而不是让工作流卡在异常里。
            self._set_status(enums_pb2.TASK_STATUS_FAILED)
            return self._result(error=error_pb2.Error(code="invalid_verdict", message=f"done 的交付节点 {deliver} 不是本轮成功的 deliver 节点"))
        self._final_verdict = verdict
        self._set_status(enums_pb2.TASK_STATUS_DONE)
        return self._result(verdict=enums_pb2.VERDICT_DONE, deliver_node=deliver, deliverable=r.result)

    def _aborted(self, verdict: dict[str, Any]) -> task_pb2.TaskWorkflowResult:
        """交付部分结果，说清完成了什么、没完成什么、为什么（附录 B.8 第 7 步）。"""
        self._final_verdict = verdict
        self._set_status(enums_pb2.TASK_STATUS_ABORTED)
        node, partial = self._delivery or ("", None)
        return self._result(verdict=enums_pb2.VERDICT_ABORT, deliver_node=node, deliverable=vd.abort_body(verdict, partial))

    async def _ask_human(self, seq: int, verdict: dict[str, Any]) -> task_pb2.HumanReplySignal | None:
        """Reviewer 升级问人（设计书 4.11）：带着状态摘要与具体问题，回复限定三种；超时或发不出问题时返回 None。"""
        assert self._input is not None
        esc = verdict["escalate"]
        options = tuple(str(o) for o in esc.get("options") or HUMAN_REPLIES)
        timeout_s = (self._stage_policy or StagePolicy()).human_timeout_s
        qid = vd.REVIEW_QUESTION_ID
        # 先登记再发问：问题刚发出就到的回复也会收下。
        self.expect_reply(qid, options)
        try:
            await workflow.execute_activity(
                names.ASK_HUMAN,
                HumanQuestion(
                    tenant=self._tenant,
                    task_id=self._input.task_id,
                    question_id=qid,
                    text=str(esc.get("question") or verdict["reason"]),
                    options=list(options),
                    deadline=(workflow.now() + timedelta(seconds=timeout_s)).isoformat(),
                    state_summary=str(esc.get("state_summary") or ""),
                    round_seq=seq,
                ),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=DECISION_RETRY,
            )
        except FailureError as err:
            self.forget_reply(qid)
            if _caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            workflow.logger.warning("Reviewer 发不出问题：%s", _failure_message(err))
            return None
        except asyncio.CancelledError:
            self.forget_reply(qid)
            raise
        try:
            reply = await self.wait_for_reply(qid, options, timedelta(seconds=timeout_s))
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.ensure_future(self._withdraw_question(seq)))
            raise
        if reply is None:
            await self._withdraw_question(seq)
        return reply

    async def _withdraw_question(self, seq: int) -> None:
        assert self._input is not None
        try:
            await workflow.execute_activity(
                names.CANCEL_HUMAN,
                HumanCancel(tenant=self._tenant, task_id=self._input.task_id, question_id=vd.REVIEW_QUESTION_ID, round_seq=seq),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=DECISION_RETRY,
            )
        except FailureError as err:
            workflow.logger.warning("撤回问题失败：%s", _failure_message(err))

    async def _run_stage(
        self,
        seq: int,
        stage: dict[str, Any],
        *,
        carried: dict[str, StageResult] | None = None,
        injections: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, StageResult]:
        """执行一个 Stage。新的运行交给 Stage Runner；S-03 留下的历史没有版本标记，按旧路径回放。
        carried 与 injections 只有 S-10 的重跑会传（见 workflows/verdicts.py）。"""
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
            task_plan=self._task_plan,
        )
        runner = StageRunner(ctx, self)
        results = await runner.run(stage, carried=carried, injections=injections)
        self._stage_policy = ctx.policy
        self._stage_cost = runner.spent_usd
        return results

    def expect_reply(self, question_id: str, options: tuple[str, ...]) -> None:
        """HumanGateway：发问之前先登记，问题刚发出就到的回复也会收下。"""
        self._waiting[question_id] = tuple(options)

    def forget_reply(self, question_id: str) -> None:
        """HumanGateway：问题没发出去时撤销登记。"""
        self._waiting.pop(question_id, None)
        self._replies.pop(question_id, None)

    async def wait_for_reply(
        self, question_id: str, options: tuple[str, ...], timeout: timedelta | None = None
    ) -> task_pb2.HumanReplySignal | None:
        """HumanGateway：等 human_reply 信号；只接受 options 里的选项；取消时等待随之结束；超过 timeout 返回 None。
        超时用 wait_condition 的持久化定时器，Worker 不占线程等待。"""
        self._waiting[question_id] = tuple(options)
        try:
            await workflow.wait_condition(lambda: question_id in self._replies, timeout=timeout)
        except asyncio.TimeoutError:
            return None
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
            artifacts=[str(a) for a in self._state.get("artifacts", [])],
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
        """收尾。S-10 起先通知 Session 工作流（交付），再写回：写回全部异步，不阻塞交付（附录 B.9）。
        S-10 之前的历史是先写回、再通知；那两行调换会改变命令顺序，重放测试据此证明自己有效。"""
        if self._s10:
            await self._notify_session(result)
            await self._finalize(result)
            return
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
                    verdict=self._final_verdict,
                    deliver_node=result.deliver_node_id,
                    artifacts=list(result.artifacts),
                    stages=list(self._stages),
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
