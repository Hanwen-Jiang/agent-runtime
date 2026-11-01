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
- 进度事件（T-11b，workflows/events.py）：每个 Round 推 round_started、stage_started、verdict，Stage Runner 推
  node_finished 与 human_input_required；交给 Go 的 Session Worker 分配 event_id、经 SSE 推给客户端。推送失败不影响 Task。

- 时间与工具调用预算（TMP 计划 eval-observability-and-runtime-fixes 第 3 节，BUDGETS_PATCH）：每个 Round 开头查，用完了问人
  （_budget，回复三种）：继续就把用完的那项再加一份，重规划时 Planner 拿到人的指导，停止或没回复就用现有结果收尾。时间只算 Agent
  在跑的时间，等人回复的时间不算。用量写进 Task State 的 budget_used 与各 Round 的 tool_calls。
- 规划与评审调用由工作流重试（同一计划第 4 节，DECISION_RETRY_PATCH，workflows/decisions.py），重试之前推 decision_retry；
  用完或到了总时限，规划用不调模型的保守规划，评审只按规则判定。
- 用量（同一计划第 11.11 节）：美元的用量是各 Round 的 cost_usd 之和，结局的 usage.cost_usd_micros 填它（token 数不填）。
  正在跑的 Round 在 task_state 查询的结果里带上已花的钱（规划加上 Stage 里已经花的），只改查询给出的副本：交给 activity 的
  Task State 不带它（Stage Runner 另外把规划的花费算进已花的钱，写进去会重复计）。不改命令序列。

Stage 由 Stage Runner 执行（S-04，workflows/stage_runner.py）。S-03 的旧执行路径保留在版本分支后面，
S-03 留下的事件历史仍按旧路径回放；S-10 之前的历史同样按旧的 Round 循环与收尾顺序回放。

- 运行中的纠偏与立刻打断（docs/plan/running-task-interaction.md 第 2 节，GUIDANCE_PATCH）：信号写进 GuidanceBook（workflows/guidance.py），事件由一个
  任务逐个推送。规划中来了纠偏：放弃这个 Stage，带上纠偏重新规划（同一轮）；打断时取消规划。Stage 跑完还有没生效的纠偏（没有 tool_loop
  接住），或者这一轮被打断：不调 Reviewer，判定由代码给出（replan，reason_code 是 user_guidance、user_interrupt）。评审中来了：不管判了什么都回
  Planner。问人中被打断：撤回问题，回 Planner。收尾之后到的拒绝。这些 replan 是用户要的，不进 Reviewer 的升级台账。
- 各轮的重要输出（同一计划第 4 节）：写进 rounds[].highlights，见 workflows/highlights.py；不改命令序列。

工作流代码必须确定：不读系统时间、不用 random、不做 I/O、不调用模型（CLAUDE.md）。
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, CancelledError, FailureError

with workflow.unsafe.imports_passed_through():
    from google.protobuf import json_format, struct_pb2

    from agentruntime.common.v1 import enums_pb2, error_pb2, metering_pb2, payload_pb2
    from agentruntime.workflow.v1 import task_pb2

    from agentruntime_worker import names
    from agentruntime_worker.activities.types import (
        FinalizeTaskInput,
        HookCall,
        HumanCancel,
        HumanQuestion,
        NodeInput,
        PlanRoundInput,
        PlanRoundOutput,
        ReviewRoundInput,
        StagePolicy,
        StagePolicyRequest,
        StageResult,
        Tenant,
    )
    from agentruntime_worker.reviewer.rules import HUMAN_REPLIES
    from agentruntime_worker.reviewer.signals import history_entry
    from agentruntime_worker.reviewer.summary import apply_state_update
    from agentruntime_worker.workflows import decisions, interrupts, patches, timeouts
    from agentruntime_worker.workflows import verdicts as vd
    from agentruntime_worker.workflows.events import EventPublisher
    from agentruntime_worker.workflows.failures import caused_by_cancel as _caused_by_cancel
    from agentruntime_worker.workflows.failures import failure_message as _failure_message
    from agentruntime_worker.workflows.guidance import INTERRUPT, NEXT_STEP, GuidanceBook
    from agentruntime_worker.workflows.highlights import round_highlights
    from agentruntime_worker.workflows.patches import (
        BUDGETS_PATCH,
        CODES_PATCH,
        DECISION_RETRY_PATCH,
        EVENTS_PATCH,
        GUIDANCE_PATCH,
        INTERRUPT_WITHDRAW_PATCH,
        POLICY_PATCH,
        PROGRESS_DETAIL_PATCH,
        R01_FIXES_PATCH,
        REVIEWER_PATCH,
        STAGE_RUNNER_PATCH,
    )
    from agentruntime_worker.workflows.stage_runner import RunContext, StageRunner, effect_name

# Round 上限：兜底。Reviewer 的升级规则与各级次数上限（S-10）让一直失败的 Task 在 12 次评审之内收尾。
MAX_ROUNDS = 20
# T-09：before_finish 最多驳回这么多次；再驳回就问人（附录 F.2：会驳回的 Hook 都有次数上限）。
MAX_FINISH_REJECTIONS = 2
# T-09 第一期只认 allow 与 deny 的挂载点（modify、inject 按 deny）；before_compact 还没接，声明了就拒绝。
STRICT_TASK_MOUNTS = ("on_task_received", "on_task_start", "before_finish")
DEFAULT_DONE_WHEN = "回答用户的问题"
DEFAULT_DONE_WHEN_CODE = "answer_question"
# message_params 里字符串的上限（defs.schema.json）。
PARAM_CHARS = 300
# 时间、工具调用预算用完时问人的问题 id（TMP 计划第 3 节）：同 _review，不合 Node id 的格式。
BUDGET_QUESTION_ID = "_budget"
_BUDGET_LABELS = {"wall_clock": ("时间预算", "分钟"), "tool_calls": ("工具调用预算", "次")}

DECISION_RETRY = timeouts.DECISION_RETRY
# 判定由代码给出时的理由（docs/plan/running-task-interaction.md 第 2 节）；客户端按 reason_code 翻译。
GUIDANCE_REASONS = {"user_guidance": "用户发来了纠偏，回 Planner 重做剩余部分", "user_interrupt": "用户打断了这一轮，回 Planner 重做剩余部分"}


def _enum_name(value: int, prefix: str, enum: Any) -> str:
    return enum.Name(value).removeprefix(prefix).lower()


def _status_name(status: int) -> str:
    return _enum_name(status, "TASK_STATUS_", enums_pb2.TaskStatus)


def _to_value(obj: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(obj, value)
    return value


def _budget_limits(inp: task_pb2.TaskWorkflowInput) -> dict[str, float]:
    """租户策略给的时间（秒）与工具调用预算；0 是不限（TMP 计划第 3 节）。"""
    wall = inp.budget.wall_clock.ToTimedelta().total_seconds() if inp.budget.HasField("wall_clock") else 0.0
    return {"wall_clock": max(0.0, wall), "tool_calls": float(max(0, inp.budget.tool_calls))}


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


def _finish_escalation(rejection: str, count: int) -> dict[str, Any]:
    """before_finish 驳回用满上限后问人的 escalate（回复限定三种，同 Reviewer 的问人）。"""
    question = f"完成前复核已经驳回 {count} 次，最近一次：{rejection}。照现状收尾、带指导重规划，还是停止并收尾？"
    return {
        "verdict": "escalate",
        "reason": f"完成前复核驳回了 {count} 次：{rejection}",
        "reason_code": "finish_rejected",
        "reason_params": {"count": count},
        "escalate": {"target": "human", "question": question, "options": list(HUMAN_REPLIES), "state_summary": f"完成前复核：{rejection}"},
    }


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
        # 最近一轮各 Node 的结果：代码给出理由的 abort 据此说清各 Node 的状态（TMP 计划第 11.5 节）。
        self._round_results: dict[str, StageResult] = {}
        self._final_verdict: dict[str, Any] | None = None
        self._stage_cost = 0.0
        # Stage 正在跑时这一轮已经花的钱（规划加上 Stage Runner 已经花的）；不在跑时为 None。只给查询与结局用。
        self._running_spent: Callable[[], float] | None = None
        # 当前的 Task Plan：交给 Stage Runner，tool_loop 的 Context Builder 据此取工作模式与阈值（只是输入）。
        self._task_plan: dict[str, Any] | None = None
        # T-09：是否走 Policy Engine 的版本分支；before_finish 已经驳回的次数。
        self._t09 = False
        self._finish_rejections = 0
        # R-01 评审之后的版本分支：没成功的 Node 也审计，tool_loop 每一步之前查预算。
        self._r01 = False
        # T-11b：进度事件；只有顶层 Task 的新运行有（版本分支 EVENTS_PATCH）。
        self._events: EventPublisher | None = None
        # TMP 计划第 3 节（BUDGETS_PATCH）：现在的上限（用完问人、继续时加一份）与原来的一份；已发出的工具调用；人选择继续的次数；
        # 上一个 Stage 的工具调用。时间只算 Agent 在跑的时间：正在等的人工问题与等人的累计秒数（几个问题同时在等只算一次）。
        self._budgets = False
        self._limits: dict[str, float] = {"wall_clock": 0.0, "tool_calls": 0.0}
        self._allotment: dict[str, float] = {"wall_clock": 0.0, "tool_calls": 0.0}
        self._tool_calls = 0
        self._extensions = 0
        self._stage_tool_calls = 0
        self._started: Any = None
        self._waiting_now: set[str] = set()
        self._waiting_since: Any = None
        self._human_wait_s = 0.0
        # 同一计划第 4 节（DECISION_RETRY_PATCH）：plan_round、review_round 由工作流重试。
        self._retry_in_workflow = False
        # 第 5 节（CODES_PATCH）：问人后停止或没回复时另推带代码的 abort verdict 事件。第 6 节：服务端定的任务族（第一个报上来的）。
        self._codes = False
        self._task_family = ""
        # docs/plan/running-task-interaction.md 第 2 节（GUIDANCE_PATCH）：纠偏的记录；工作流开始之前到的信号；正在跑的 Stage；推送事件的任务。
        self._book: GuidanceBook | None = None
        self._early_guidance: list[task_pb2.TaskGuidanceSignal] = []
        self._runner: StageRunner | None = None
        self._guidance_publisher: asyncio.Future[None] | None = None
        self._events_decided = False
        self._closing = False
        # 上一个 Stage 被打断了；工具调用的目标（第 4 节）。
        self._stage_interrupted = False
        self._stage_reads: list[str] = []
        self._stage_writes: list[str] = []

    @workflow.run
    async def run(self, inp: task_pb2.TaskWorkflowInput) -> task_pb2.TaskWorkflowResult:
        # 新的运行在第一个工作流任务里一次问完全部版本分支：local activity 之后不再第一次问（workflows/patches.py）。
        patches.at_start()
        self._input = inp
        self._tenant = Tenant(
            tenant_id=inp.tenant.tenant_id,
            user_id=inp.tenant.user_id,
            workspace_id=inp.tenant.workspace_id,
            session_id=inp.tenant.session_id,
        )
        self._state = _initial_state(inp)
        # TMP 计划第 3、4 节的版本分支：新的运行在 at_start 里已经问过，这里只取值；之前的历史没有标记，都是 False。
        self._budgets = workflow.patched(BUDGETS_PATCH)
        self._retry_in_workflow = workflow.patched(DECISION_RETRY_PATCH)
        self._codes = workflow.patched(CODES_PATCH)
        if workflow.patched(GUIDANCE_PATCH):
            self._book = GuidanceBook()
            self._guidance_publisher = asyncio.ensure_future(self._publish_guidance())
            for early in self._early_guidance:
                self._on_guidance(early)
        if self._budgets:
            self._started = workflow.now()
            self._limits = _budget_limits(inp)
            self._allotment = dict(self._limits)
            self._write_limits()
            self._refresh_used()
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

    @workflow.signal(name=names.TASK_GUIDANCE_SIGNAL)
    def task_guidance(self, signal: task_pb2.TaskGuidanceSignal) -> None:
        """用户发来纠偏、立刻打断，或者撤回、修改一条（docs/plan/running-task-interaction.md 第 2 节）。信号可以先于工作流的第一步到达；
        没有 GUIDANCE_PATCH 的旧运行只记日志，界面等 task_finished 再提示没有生效。文字不进日志。"""
        if self._input is None:
            self._early_guidance.append(signal)
        elif self._book is None:
            workflow.logger.warning("这个运行早于纠偏，忽略信号：%s", signal.guidance_id)
        else:
            self._on_guidance(signal)

    def _on_guidance(self, signal: task_pb2.TaskGuidanceSignal) -> None:
        book = self._book
        assert book is not None
        if signal.kind == task_pb2.TASK_GUIDANCE_KIND_EDIT:
            book.edit(signal.guidance_id, signal.text)
        elif signal.kind == task_pb2.TASK_GUIDANCE_KIND_RETRACT:
            book.retract(signal.guidance_id)
        else:
            mode = INTERRUPT if signal.mode == enums_pb2.GUIDANCE_MODE_INTERRUPT else NEXT_STEP
            in_flight = self._runner is not None and self._runner.irreversible_in_flight()
            if book.submit(signal.guidance_id, signal.text, mode, in_flight=in_flight):
                for scope in list(book.scopes):
                    scope.interrupt()
                if self._runner is not None:
                    self._runner.interrupt()
            if book.items:
                self._state.setdefault("guidance", book.items)

    async def _publish_guidance(self) -> None:
        """按发生的先后逐个推 guidance_updated。_closing 之后推完就结束。"""
        book = self._book
        assert book is not None
        number = 0
        while True:
            await workflow.wait_condition(lambda: (bool(book.outbox) and self._events_decided) or self._closing)
            if not book.outbox:
                return
            event = book.outbox.pop(0)
            if self._events is not None:
                number += 1
                await self._events.guidance_updated(max(1, self._round_seq), event, number)

    async def _guidance_published(self) -> None:
        if self._guidance_publisher is not None:
            self._events_decided = self._closing = True
            await asyncio.shield(self._guidance_publisher)

    def _heartbeat(self) -> timedelta | None:
        return timeouts.INTERRUPTIBLE_HEARTBEAT if self._book is not None else None

    async def _interruptible(self, call: Any) -> Any:
        """能被立刻打断的调用：被打断时抛 interrupts.Interrupted。之前的运行直接等。"""
        return await call if self._book is None else await interrupts.run(self._book.scopes, call)

    @workflow.query(name=names.TASK_STATE_QUERY)
    def task_state(self) -> task_pb2.TaskStateQueryResult:
        result = task_pb2.TaskStateQueryResult(
            task_id=self._input.task_id if self._input else "",
            status=self._status,
            round_seq=self._round_seq,
        )
        if self._state:
            state = self._state_with_running_cost()
            if state.get("done_when") == DEFAULT_DONE_WHEN:
                # 默认的说明带代码给客户端翻译。只在查询里带：Planner 的提示整份带 Task State，模型不该看到它（TMP 计划第 11.5 节）。
                state = {**state, "done_when_code": DEFAULT_DONE_WHEN_CODE}
            result.task_state.CopyFrom(payload_pb2.JsonPayload(schema_id=names.TASK_STATE_SCHEMA_ID, value=_to_value(state)))
        return result

    def _state_with_running_cost(self) -> dict[str, Any]:
        """查询给出的 Task State：Stage 正在跑时，这一轮带上已经花的钱（副本，不改工作流里的 Task State）。"""
        rounds = self._state.get("rounds") or []
        if self._running_spent is None or not rounds or "cost_usd" in rounds[-1]:
            return self._state
        return {**self._state, "rounds": [*rounds[:-1], {**rounds[-1], "cost_usd": round(self._running_spent(), 6)}]}

    def _spent_usd(self) -> float:
        """Task 已经花的钱：各 Round 的 cost_usd 之和，正在跑的那一轮算上已经花的。"""
        return sum(float(r.get("cost_usd") or 0.0) for r in self._state_with_running_cost().get("rounds") or [])

    # --- TMP 计划第 3 节：时间与工具调用预算 ---

    def _active_s(self) -> float:
        """Task 开始以来 Agent 在跑的秒数（工作流时间）：减去等人回复的时间。"""
        if self._started is None:
            return 0.0
        now = workflow.now()
        waiting = (now - self._waiting_since).total_seconds() if self._waiting_now and self._waiting_since is not None else 0.0
        return max(0.0, (now - self._started).total_seconds() - self._human_wait_s - waiting)

    def _wait_begins(self, question_id: str) -> None:
        if question_id in self._waiting_now:
            return
        if not self._waiting_now:
            self._waiting_since = workflow.now()
        self._waiting_now.add(question_id)

    def _wait_ends(self, question_id: str) -> None:
        if question_id not in self._waiting_now:
            return
        self._waiting_now.discard(question_id)
        if not self._waiting_now and self._waiting_since is not None:
            self._human_wait_s += (workflow.now() - self._waiting_since).total_seconds()
            self._waiting_since = None

    def _write_limits(self) -> None:
        constraints = self._state.setdefault("constraints", {})
        if self._limits["tool_calls"] > 0:
            constraints["tool_calls"] = int(self._limits["tool_calls"])
        if self._limits["wall_clock"] > 0:
            constraints["wall_clock_min"] = round(self._limits["wall_clock"] / 60, 4)
        if not constraints:
            self._state.pop("constraints", None)

    def _refresh_used(self) -> None:
        if self._budgets and self._state:
            self._state["budget_used"] = {
                "wall_clock_min": round(self._active_s() / 60, 4), "tool_calls": self._tool_calls, "extensions": self._extensions,
            }

    def _exhausted(self) -> list[str]:
        """用完了的预算（时间在前）；不限的不算。"""
        out = []
        if self._limits["wall_clock"] > 0 and self._active_s() >= self._limits["wall_clock"]:
            out.append("wall_clock")
        if self._limits["tool_calls"] > 0 and self._tool_calls >= self._limits["tool_calls"]:
            out.append("tool_calls")
        return out

    def _budget_params(self, kind: str) -> dict[str, Any]:
        if kind == "wall_clock":
            return {"budget": kind, "limit": round(self._limits[kind] / 60, 2), "used": round(self._active_s() / 60, 2)}
        return {"budget": kind, "limit": int(self._limits[kind]), "used": self._tool_calls}

    async def _budget_gate(self, seq: int, last_results: dict[str, StageResult]) -> tuple[task_pb2.TaskWorkflowResult | None, task_pb2.HumanReplySignal | None]:
        """Round 开头：预算用完了就问人。返回 (收尾的结果, 人的回复)：没用完时两项都是 None；人选择继续或重规划时，
        用完的那几项各加一份，返回回复；停止或没有回复时用现有结果收尾，返回结果。"""
        self._refresh_used()
        kinds = self._exhausted()
        if not kinds:
            return None, None
        params = self._budget_params(kinds[0])
        label, unit = _BUDGET_LABELS[kinds[0]]
        text = (
            f"{label} {params['limit']:g} {unit}已经用完（已用 {params['used']:g} {unit}）。"
            "原样继续（再加一份同样的预算）、带指导重规划，还是停止并用现有结果收尾？"
        )
        summary = f"已跑 {seq - 1} 轮，{label}用完"
        try:
            reply = await self._interruptible(self._ask(seq, BUDGET_QUESTION_ID, text, HUMAN_REPLIES, summary, code="budget_exhausted", params=params))
        except interrupts.Interrupted:
            # 用户没回答预算的问题，直接打断要接着做：按“带指导重规划”处理（Planner 看得到纠偏）。
            reply = task_pb2.HumanReplySignal(question_id=BUDGET_QUESTION_ID, option="replan_with_guidance")
        if reply is None or reply.option == "stop_and_wrap_up":
            why = "问人没有得到回复：按停止处理" if reply is None else "人选择停止"
            verdict = vd.stopped(last_results, f"{label}用完，{why}，用现有结果收尾")
            verdict["reason_code"] = "budget_no_reply" if reply is None else "budget_stopped"
            verdict["reason_params"] = {"budget": kinds[0]}
            if self._events is not None:
                await self._events.verdict(seq, verdict)
            return self._aborted(verdict), None
        for kind in kinds:
            self._limits[kind] += self._allotment[kind]
        self._extensions += 1
        self._write_limits()
        self._refresh_used()
        return None, reply

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
            # T-09 又改变了命令序列（规划前加载 StagePolicy、Task 级挂载点、before_finish）：新的运行走版本分支。
            self._t09 = workflow.patched(POLICY_PATCH)
            self._r01 = workflow.patched(R01_FIXES_PATCH)
            if self._t09:
                refused = await self._task_start(inputs)
                if refused is not None:
                    return refused
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
        # T-11b 加入了推送进度事件的 activity：新的运行走版本分支；子 Task 没有自己的事件流，不推。
        if workflow.patched(EVENTS_PATCH) and self._input.depth == 0:
            # 2026-10-28 起的运行另推 node_started 与 tool_status（版本分支 PROGRESS_DETAIL_PATCH）。
            self._events = EventPublisher(self._tenant, self._input.task_id, detail=workflow.patched(PROGRESS_DETAIL_PATCH))
        self._events_decided = True
        task_plan: dict[str, Any] | None = None
        last_round: dict[str, Any] | None = None
        rerun: vd.Rerun | None = None
        # 上一轮的结果：预算用完、人选择停止时据此收尾。
        last_results: dict[str, StageResult] = {}
        # 当前 Stage 的规划：原地重跑不经 Planner，goal、exit_when 还是它的文字，进度事件也沿用它的代码（TMP 计划第 11.5 节）。
        plan: PlanRoundOutput | None = None
        for seq in range(1, MAX_ROUNDS + 1):
            self._round_seq = seq
            carried: dict[str, StageResult] = {}
            injections: dict[str, list[dict[str, Any]]] = {}
            if self._events is not None:
                await self._events.round_started(seq, rerun=rerun is not None)
            if self._budgets:
                # TMP 计划第 3 节：时间或工具调用预算用完了先问人。
                stopped, reply = await self._budget_gate(seq, last_results)
                if stopped is not None:
                    return stopped
                if reply is not None and reply.option == "replan_with_guidance":
                    rerun = None
                    last_round = {**(last_round or {}), "human_reply": {"option": reply.option, "guidance": reply.guidance}}
            if self._book is not None and self._book.has_pending():
                # 原地重跑不经 Planner，纠偏要回 Planner。
                rerun = None
            triage_level: str | None = None
            # 这一轮规划的花费：计入 Round 的成本，Stage 执行时已经算进已花的钱（成本口径的人定方案）。原地重跑不经 Planner。
            plan_cost = 0.0
            if rerun is None:
                plan, plan_cost = await self._planned(seq, inputs, pinned, task_plan, last_round)
                stage = plan.stage
                triage_level = plan.triage_level
                if plan.task_family and not self._task_family:
                    # TMP 计划第 6 节：任务族由第一轮分诊定，之后的重规划不改。
                    self._task_family = plan.task_family
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
            if self._events is not None:
                await self._events.stage_started(seq, stage, triage_level, plan)

            results = await self._run_stage(seq, stage, carried=carried, injections=injections, plan_cost=plan_cost)
            round_entry["cost_usd"] = round(plan_cost + self._stage_cost, 6)
            last_results = results
            self._round_results = results
            if self._budgets:
                round_entry["tool_calls"] = self._stage_tool_calls
                self._tool_calls += self._stage_tool_calls
                self._refresh_used()
            self._stages.append(vd.stage_summary(stage))

            round_entry["status"] = "reviewing"
            verdict, update, review_cost, highlights = await self._judged(seq, stage, results)
            round_entry["cost_usd"] = round(round_entry["cost_usd"] + review_cost, 6)
            if self._events is not None:
                await self._events.verdict(seq, verdict)
            self._record_highlights(round_entry, highlights, results)
            verdict = await self._guidance_arrived_meanwhile(seq, verdict, results)
            kind = verdict["verdict"]
            round_entry["status"] = "completed"
            round_entry["verdict"] = kind
            apply_state_update(self._state, update)
            as_dicts = {nid: dataclasses.asdict(r) for nid, r in results.items()}
            if verdict.get("reason_code") not in GUIDANCE_REASONS:
                # 用户要的 replan 不进 Reviewer 的升级台账。
                self._history.append(history_entry(seq, verdict, as_dicts))
            self._remember_delivery(results)
            last_round = {"stage": stage, "results": as_dicts, "verdict": verdict}

            if kind == "continue":
                self._checkpoints[seq] = vd.snapshot(self._state)
            elif kind == "done":
                rejection = await self._before_finish(stage, verdict, results) if self._t09 else ""
                if not rejection:
                    return self._done(verdict, results)
                self._finish_rejections += 1
                last_round["finish_rejected"] = rejection
                if self._finish_rejections > MAX_FINISH_REJECTIONS:
                    # 驳回用满上限：不再驳回，问人（同 Reviewer 的 escalate）。
                    try:
                        reply = await self._interruptible(self._ask_human(seq, _finish_escalation(rejection, self._finish_rejections)))
                    except interrupts.Interrupted:
                        await self._replan_interrupted(seq, round_entry, results, last_round)
                        continue
                    if reply is None:
                        return await self._stopped(seq, vd.stopped(results, f"完成前复核驳回后问人没有得到回复：{rejection}；按停止处理"), "human_no_reply")
                    if reply.option == "stop_and_wrap_up":
                        return await self._stopped(seq, vd.stopped(results, f"完成前复核驳回后人选择停止并收尾：{rejection}"), "human_stopped")
                    if reply.option == "continue_as_is":
                        return self._done(verdict, results)
                    last_round["human_reply"] = {"option": reply.option, "guidance": reply.guidance}
                # 驳回：下一轮回 Planner，它看得到驳回的理由。
            elif kind == "abort":
                return self._aborted(verdict)
            elif (kind == "repair" and verdict["repair"].get("strategy") == "retry_with_error") or (kind == "escalate" and verdict["escalate"].get("target") == "tier"):
                rerun = vd.rerun(stage, results, verdict, seq + 1)
            elif kind == "escalate":
                try:
                    reply = await self._interruptible(self._ask_human(seq, verdict))
                except interrupts.Interrupted:
                    await self._replan_interrupted(seq, round_entry, results, last_round)
                    continue
                if reply is None:
                    return await self._stopped(seq, vd.stopped(results, f"问人没有得到回复：{verdict['reason']}；按停止处理，用现有结果收尾"), "human_no_reply")
                if reply.option == "stop_and_wrap_up":
                    return await self._stopped(seq, vd.stopped(results, f"人选择停止并用现有结果收尾：{verdict['reason']}"), "human_stopped")
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

    # --- 纠偏与立刻打断在 Round 循环里的去向（docs/plan/running-task-interaction.md 第 2 节） ---

    async def _planned(
        self, seq: int, inputs: dict[str, Any], pinned: dict[str, Any], task_plan: dict[str, Any] | None, last_round: dict[str, Any] | None
    ) -> tuple[PlanRoundOutput, float]:
        """规划。规划中来了纠偏：规划完放弃这个 Stage，带上纠偏重新规划；打断时取消规划。放弃的规划也花了钱，算进这一轮（被取消的
        那次用量不明，不算）。返回 (规划, 这一轮规划的花费)。"""
        book = self._book
        if book is None:
            plan = await self._plan(seq, inputs, pinned, task_plan, last_round)
            return plan, float(plan.cost_usd or 0.0)
        cost = 0.0
        while True:
            book.apply_pending(seq)
            try:
                plan = await self._interruptible(self._plan(seq, inputs, pinned, task_plan, last_round))
            except interrupts.Interrupted:
                continue
            cost += float(plan.cost_usd or 0.0)
            if not book.has_pending():
                return plan, cost

    def _guidance_verdict(self, seq: int, results: dict[str, StageResult], *, interrupted: bool = False) -> dict[str, Any]:
        """判定由代码给出：回 Planner。待生效的纠偏这时生效。"""
        book = self._book
        assert book is not None
        code = "user_interrupt" if interrupted or book.has_pending_interrupt() else "user_guidance"
        book.apply_pending(seq)
        return vd.guidance_replan(results, code, GUIDANCE_REASONS[code])

    async def _judged(self, seq: int, stage: dict[str, Any], results: dict[str, StageResult]) -> tuple[dict[str, Any], dict[str, Any], float, dict[str, Any] | None]:
        """这一轮的判定。被打断的轮、Stage 跑完还有没生效的纠偏的轮不调 Reviewer；评审中来了纠偏，不管判了什么都回 Planner。"""
        book = self._book
        if book is None:
            return await self._review(seq, stage, results)
        if self._stage_interrupted or book.has_pending():
            return self._guidance_verdict(seq, results, interrupted=self._stage_interrupted), {}, 0.0, None
        try:
            verdict, update, cost, highlights = await self._interruptible(self._review(seq, stage, results))
        except interrupts.Interrupted:
            return self._guidance_verdict(seq, results, interrupted=True), {}, 0.0, None
        if book.has_pending():
            verdict = self._guidance_verdict(seq, results)
        return verdict, update, cost, highlights

    async def _guidance_arrived_meanwhile(self, seq: int, verdict: dict[str, Any], results: dict[str, StageResult]) -> dict[str, Any]:
        """推 verdict 事件的时候来了纠偏：判了 done、abort 也不收尾，回 Planner（别的去向下一轮规划时自然带上）。"""
        book = self._book
        if book is None or verdict["verdict"] not in ("done", "abort") or not book.has_pending():
            return verdict
        changed = self._guidance_verdict(seq, results)
        if self._events is not None:
            await self._events.verdict(seq, changed, about="guidance")
        return changed

    async def _replan_interrupted(self, seq: int, round_entry: dict[str, Any], results: dict[str, StageResult], last_round: dict[str, Any]) -> None:
        """问人的时候被打断：问题已经撤回，回 Planner。"""
        changed = self._guidance_verdict(seq, results, interrupted=True)
        round_entry["verdict"] = changed["verdict"]
        last_round["verdict"] = changed
        if self._events is not None:
            await self._events.verdict(seq, changed, about="guidance")

    def _record_highlights(self, round_entry: dict[str, Any], raw: dict[str, Any] | None, results: dict[str, StageResult]) -> None:
        """docs/plan/running-task-interaction.md 第 4 节：这一轮的重要输出写进 Task State。"""
        deliverable = next((results[nid].result for nid in sorted(results) if results[nid].role == "deliver" and results[nid].status == "succeeded"), None)
        highlights = round_highlights(raw, reads=self._stage_reads, writes=self._stage_writes, deliverable=deliverable)
        if highlights:
            round_entry["highlights"] = highlights

    # --- T-09：Task 级挂载点（细化见 docs/plan/policy-engine.md 第 2 节第 4 条） ---

    async def _task_start(self, inputs: dict[str, Any]) -> task_pb2.TaskWorkflowResult | None:
        """规划之前：加载 StagePolicy（各 Stage 共用），执行 on_task_received、on_task_start 的 Hook。
        租户策略取不到、before_compact 声明了 Hook（第一期没接）、Hook 拒绝，Task 都以 abort 结束、不规划；否则返回 None。"""
        assert self._input is not None
        try:
            self._stage_policy = await workflow.execute_activity(
                names.LOAD_STAGE_POLICY,
                StagePolicyRequest(tenant=self._tenant, task_id=self._input.task_id, pinned=self._pinned),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=DECISION_RETRY,
                result_type=StagePolicy,
            )
        except FailureError as err:
            if _caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            return self._refuse(f"租户策略取不到（{_failure_message(err)}），按拒绝处理")
        hooks = self._stage_policy.hooks
        if hooks.get("before_compact"):
            return self._refuse(f"策略在 before_compact 声明了 Hook {hooks['before_compact']}，第一期还没有接上这个挂载点，按拒绝处理")
        for mount, subject in (
            ("on_task_received", {"message": self._input.message, "inputs": inputs}),
            ("on_task_start", {"task_state": self._state}),
        ):
            reason = await self._task_hooks(mount, list(hooks.get(mount, [])), subject)
            if reason:
                return self._refuse(reason)
        return None

    def _refuse(self, reason: str) -> task_pb2.TaskWorkflowResult:
        """Task 级的拒绝：不规划，以 abort 收尾。"""
        return self._aborted({
            "verdict": "abort",
            "reason": reason,
            "reason_code": "policy_refused",
            "reason_params": {"detail": reason[:PARAM_CHARS]},
            "abort": {"completed": [], "not_completed": ["整个 Task（被 Policy Engine 拒绝）"]},
        })

    async def _task_hooks(self, mount: str, hooks: list[str], subject: dict[str, Any]) -> str:
        """在 Task 级挂载点执行 Hook，返回拒绝的理由（放行时为空）。Hook 执行不了按拒绝；第一期这些挂载点只认 allow 与 deny，
        modify、inject 也按拒绝（STRICT_TASK_MOUNTS）。没有 Hook 时不执行（不产生命令）。"""
        assert self._input is not None
        if not hooks:
            return ""
        try:
            results = await workflow.execute_local_activity(
                names.RUN_HOOKS,
                HookCall(tenant=self._tenant, task_id=self._input.task_id, round_seq=self._round_seq, mount=mount, hooks=hooks, subject=subject),
                start_to_close_timeout=timeouts.HOOK_START_TO_CLOSE,
                retry_policy=timeouts.HOOK_RETRY,
                result_type=list,
            )
        except FailureError as err:
            if _caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            return f"{mount} 的 Hook 执行失败：{_failure_message(err)}"
        for r in results:
            decision = r.get("decision")
            if decision == "deny":
                return f"{r.get('hook')}：{r.get('reason') or '被拒绝'}"
            if decision != "allow" and mount in STRICT_TASK_MOUNTS:
                return f"{r.get('hook')}：在 {mount} 返回了 {decision}，第一期这个挂载点只认 allow 与 deny，按拒绝处理"
        return ""

    async def _before_finish(self, stage: dict[str, Any], verdict: dict[str, Any], results: dict[str, StageResult]) -> str:
        """Reviewer 判 done 之后、收尾之前：执行 before_finish 的 Hook，返回驳回的理由（放行时为空）。"""
        hooks = list((self._stage_policy or StagePolicy()).hooks.get("before_finish", []))
        deliver = str((verdict.get("done") or {}).get("deliver_node", ""))
        r = results.get(deliver)
        node = next((n for n in stage.get("nodes", []) if n.get("id") == deliver), {})
        subject = {
            "verdict": verdict,
            "deliver_node": deliver,
            "deliver_status": r.status if r is not None else "missing",
            "deliverable": r.result if r is not None else None,
            "output_schema": node.get("output_schema"),
        }
        return await self._task_hooks("before_finish", hooks, subject)

    async def _plan(
        self, seq: int, inputs: dict[str, Any], pinned: dict[str, Any], task_plan: dict[str, Any] | None, last_round: dict[str, Any] | None
    ) -> PlanRoundOutput:
        assert self._input is not None
        inp = self._input
        request = PlanRoundInput(
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
        )
        if self._retry_in_workflow:
            return await self._decide(names.PLAN_ROUND, request, dataclasses.replace(request, no_model=True), "plan_round", seq, PlanRoundOutput)
        return await workflow.execute_activity(
            names.PLAN_ROUND,
            request,
            task_queue=names.DECISION_QUEUE,
            start_to_close_timeout=timeouts.MODEL_DECISION_START_TO_CLOSE,
            retry_policy=DECISION_RETRY,
            result_type=PlanRoundOutput,
        )

    async def _decide(self, activity: str, request: Any, without_model: Any, step: str, seq: int, result_type: type) -> Any:
        """TMP 计划第 4 节：由工作流重试决策调用，重试之前推 decision_retry；用完或到了总时限，用不调模型的请求再调一次。"""
        try:
            return await decisions.run(
                activity, request, task_queue=names.DECISION_QUEUE, result_type=result_type, step=step, round_seq=seq, events=self._events,
                heartbeat_timeout=self._heartbeat(),
            )
        except decisions.DecisionExhausted as exhausted:
            workflow.logger.warning("%s 试了 %d 次都没成（%s），改用不调模型的做法", step, exhausted.attempts, _failure_message(exhausted.last))
        return await workflow.execute_activity(
            activity,
            without_model,
            task_queue=names.DECISION_QUEUE,
            start_to_close_timeout=timeouts.NO_MODEL_START_TO_CLOSE,
            retry_policy=DECISION_RETRY,
            result_type=result_type,
        )

    async def _review(self, seq: int, stage: dict[str, Any], results: dict[str, StageResult]) -> tuple[dict[str, Any], dict[str, Any], float, dict[str, Any] | None]:
        """Reviewer：输入只有 Stage 结果（含本轮执行的 Stage）、Task State、验收条件与之前各 Round 的摘要。
        返回 (Verdict, 要写进 Task State 的汇总, 这次评审的花费, 这一轮的重要输出)。"""
        assert self._input is not None
        request = ReviewRoundInput(
            tenant=self._tenant,
            task_id=self._input.task_id,
            round_seq=seq,
            task_state=self._state,
            stage_results=results,
            done_when=self._state["done_when"],
            stage=stage,
            history=list(self._history),
        )
        if self._retry_in_workflow:
            raw = await self._decide(names.REVIEW_ROUND, request, dataclasses.replace(request, rules_only=True), "review_round", seq, dict)
        else:
            raw = await workflow.execute_activity(
                names.REVIEW_ROUND,
                request,
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.MODEL_DECISION_START_TO_CLOSE,
                retry_policy=DECISION_RETRY,
                result_type=dict,
            )
        verdict, update = vd.review_output(raw)
        return verdict, update, vd.review_cost(raw), vd.review_highlights(raw)

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

    async def _stopped(self, seq: int, verdict: dict[str, Any], code: str) -> task_pb2.TaskWorkflowResult:
        """问人后停止或没有回复：用现有结果收尾。CODES_PATCH 之后的运行理由带代码，并另推一个 abort 的 verdict 事件
        （TMP 计划第 5 节），客户端看得到为什么收尾。"""
        if self._codes:
            verdict["reason_code"] = code
            if self._events is not None:
                # 这个 Round 已经推过 Reviewer 的 verdict：另用一个去重键，不被当成重复的。
                await self._events.verdict(seq, verdict, about="stopped")
        return self._aborted(verdict)

    def _aborted(self, verdict: dict[str, Any]) -> task_pb2.TaskWorkflowResult:
        """交付部分结果，说清完成了什么、没完成什么、为什么（附录 B.8 第 7 步）。"""
        self._final_verdict = verdict
        self._set_status(enums_pb2.TASK_STATUS_ABORTED)
        node, partial = self._delivery or ("", None)
        return self._result(verdict=enums_pb2.VERDICT_ABORT, deliver_node=node, deliverable=vd.abort_body(verdict, partial, self._round_results))

    async def _ask_human(self, seq: int, verdict: dict[str, Any]) -> task_pb2.HumanReplySignal | None:
        """Reviewer 升级问人（设计书 4.11）：带着状态摘要与具体问题，回复限定三种；超时或发不出问题时返回 None。"""
        esc = verdict["escalate"]
        options = tuple(str(o) for o in esc.get("options") or HUMAN_REPLIES)
        text = str(esc.get("question") or verdict["reason"])
        code = str(verdict.get("reason_code") or "")
        return await self._ask(seq, vd.REVIEW_QUESTION_ID, text, options, str(esc.get("state_summary") or ""), code=code, params=verdict.get("reason_params"))

    async def _ask(
        self, seq: int, qid: str, text: str, options: tuple[str, ...], state_summary: str, *, code: str = "", params: dict[str, Any] | None = None,
    ) -> task_pb2.HumanReplySignal | None:
        """经 HumanChannel 问人，等回复：超时或发不出问题时返回 None。Reviewer 的升级与预算用完（TMP 计划第 3 节）共用。"""
        assert self._input is not None
        timeout_s = (self._stage_policy or StagePolicy()).human_timeout_s
        deadline = workflow.now() + timedelta(seconds=timeout_s)
        # 先登记再发问：问题刚发出就到的回复也会收下。
        self.expect_reply(qid, options)
        try:
            await workflow.execute_activity(
                names.ASK_HUMAN,
                HumanQuestion(
                    tenant=self._tenant,
                    task_id=self._input.task_id,
                    question_id=qid,
                    text=text,
                    options=list(options),
                    deadline=deadline.isoformat(),
                    state_summary=state_summary,
                    round_seq=seq,
                ),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=DECISION_RETRY,
            )
        except FailureError as err:
            self.forget_reply(qid)
            if _caused_by_cancel(err):
                await self._withdraw_if_interrupted(seq, qid)
                raise asyncio.CancelledError() from err
            workflow.logger.warning("发不出问题 %s：%s", qid, _failure_message(err))
            return None
        except asyncio.CancelledError:
            self.forget_reply(qid)
            await self._withdraw_if_interrupted(seq, qid)
            raise
        if self._book is not None and interrupts.asked_to_cancel():
            # 发问的 activity 正好结束时来的取消被它吞了（见 stage_runner._ask_and_wait）：问题已经发出去了，撤回。
            self.forget_reply(qid)
            await self._withdraw_question(seq, qid)
            raise asyncio.CancelledError()
        try:
            if self._events is not None:
                await self._events.human_input_required(seq, qid, "escalation", text, options, deadline.isoformat(), code=code, params=params)
        except asyncio.CancelledError:
            self.forget_reply(qid)
            await self._withdraw_if_interrupted(seq, qid)
            raise
        try:
            reply = await self.wait_for_reply(qid, options, timedelta(seconds=timeout_s))
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.ensure_future(self._withdraw_question(seq, qid)))
            raise
        if reply is None:
            await self._withdraw_question(seq, qid)
        return reply

    async def _withdraw_if_interrupted(self, seq: int, qid: str) -> None:
        """发问的 activity 还没结束就被取消：问题可能已经发出去了，撤回（工作流不等这个 activity 结束）。"""
        if self._book is not None and workflow.patched(INTERRUPT_WITHDRAW_PATCH):
            await asyncio.shield(asyncio.ensure_future(self._withdraw_question(seq, qid)))

    async def _withdraw_question(self, seq: int, qid: str = vd.REVIEW_QUESTION_ID) -> None:
        assert self._input is not None
        try:
            await workflow.execute_activity(
                names.CANCEL_HUMAN,
                HumanCancel(tenant=self._tenant, task_id=self._input.task_id, question_id=qid, round_seq=seq),
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
        plan_cost: float = 0.0,
    ) -> dict[str, StageResult]:
        """执行一个 Stage。新的运行交给 Stage Runner；S-03 留下的历史没有版本标记，按旧路径回放。
        carried 与 injections 只有 S-10 的重跑会传（见 workflows/verdicts.py）；plan_cost 是这一轮规划的花费，算进已花的钱。"""
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
            t09=self._t09,
            budget_usd=self._input.budget.usd_micros / 1_000_000 if self._input.budget.usd_micros else None,
            spent_before_usd=round(sum(float(r.get("cost_usd") or 0) for r in self._state.get("rounds", [])) + plan_cost, 6),
            events=self._events,
            r01=self._r01,
            budgets=self._budgets,
            tool_calls_limit=int(self._limits["tool_calls"]),
            tool_calls_before=self._tool_calls,
            wall_clock_limit_s=self._limits["wall_clock"],
            active_s=self._active_s if self._budgets else None,
            decision_retry=self._retry_in_workflow,
            interruptible=self._book is not None,
            guidance=self._book,
        )
        runner = StageRunner(ctx, self)
        self._running_spent = lambda: plan_cost + runner.spent_usd
        self._runner = runner
        try:
            results = await runner.run(stage, carried=carried, injections=injections)
        finally:
            self._running_spent = None
            self._runner = None
        self._stage_policy = ctx.policy
        self._stage_cost = runner.spent_usd
        self._stage_tool_calls = runner.tool_calls
        self._stage_interrupted = runner.interrupted
        self._stage_reads, self._stage_writes = runner.reads, runner.writes
        return results

    def expect_reply(self, question_id: str, options: tuple[str, ...]) -> None:
        """HumanGateway：发问之前先登记，问题刚发出就到的回复也会收下。从这时起到收到回复不算 Agent 在跑的时间。"""
        self._waiting[question_id] = tuple(options)
        self._wait_begins(question_id)

    def forget_reply(self, question_id: str) -> None:
        """HumanGateway：问题没发出去时撤销登记。"""
        self._waiting.pop(question_id, None)
        self._replies.pop(question_id, None)
        self._wait_ends(question_id)

    async def wait_for_reply(
        self, question_id: str, options: tuple[str, ...], timeout: timedelta | None = None
    ) -> task_pb2.HumanReplySignal | None:
        """HumanGateway：等 human_reply 信号；只接受 options 里的选项；取消时等待随之结束；超过 timeout 返回 None。
        超时用 wait_condition 的持久化定时器，Worker 不占线程等待。"""
        self._waiting[question_id] = tuple(options)
        self._wait_begins(question_id)
        try:
            await workflow.wait_condition(lambda: question_id in self._replies, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiting.pop(question_id, None)
            self._wait_ends(question_id)
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
        self._refresh_used()
        result = task_pb2.TaskWorkflowResult(
            task_id=self._input.task_id,
            status=self._status,
            final_verdict=verdict,
            deliver_node_id=deliver_node,
            rounds=len(self._state.get("rounds", [])),
            artifacts=[str(a) for a in self._state.get("artifacts", [])],
            usage=metering_pb2.Usage(cost_usd_micros=round(self._spent_usd() * 1_000_000)),
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
        S-10 之前的历史是先写回、再通知；那两行调换会改变命令顺序，重放测试据此证明自己有效。
        进入收尾起纠偏一律拒绝；结束之前把 guidance_updated 都推完。"""
        if self._book is not None:
            self._book.close()
        if self._s10:
            await self._notify_session(result)
            if self._t09:
                await self._task_end(result)
            await self._finalize(result)
            await self._guidance_published()
            return
        await self._finalize(result)  # replay-mutation:A
        await self._notify_session(result)  # replay-mutation:B

    async def _task_end(self, result: task_pb2.TaskWorkflowResult) -> None:
        """T-09：on_task_end 的 Hook 在收尾时执行；结局已定，结果只记录，deny 不改变结局。"""
        hooks = list((self._stage_policy or StagePolicy()).hooks.get("on_task_end", []))
        subject = {"status": enums_pb2.TaskStatus.Name(result.status), "verdict": enums_pb2.Verdict.Name(result.final_verdict)}
        reason = await self._task_hooks("on_task_end", hooks, subject)
        if reason:
            workflow.logger.warning("on_task_end 的 Hook 没有放行（不改变结局）：%s", reason)

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
                    # 服务端定的任务族优先（TMP 计划第 6 节），没有时用 Task Plan 里的。
                    task_family=self._task_family or str(((self._task_plan or {}).get("triage") or {}).get("task_family") or ""),
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
