"""Stage Runner 集成测试的支架：脚本化的 Planner、Reviewer、执行器、Catalog、Recipe、tool_loop 与 Policy Engine。

Task 工作流、SubAgentWorkflow、SubgraphWorkflow 用真实代码跑在 Temporal 本地开发服务上；activity 按名字登记在真实的
任务队列上，由测试脚本决定返回什么，并记下每次调用的输入、开始与结束的先后、所在的工作流与队列。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import itertools
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest_asyncio
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentruntime.workflow.v1 import task_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.types import (
    CatalogInfo,
    CatalogRequest,
    FinalizeTaskInput,
    HookCall,
    HumanCancel,
    HumanQuestion,
    NodeInput,
    PlanRoundInput,
    PlanRoundOutput,
    ReconcileResult,
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
from agentruntime_worker.workflows.stage_runner import SubAgentWorkflow, SubgraphWorkflow
from agentruntime_worker.workflows.task import TaskWorkflow

from .support import task_input, validator

Behavior = Callable[[NodeInput, "Script"], Awaitable[dict[str, Any]]]


@dataclass
class Call:
    activity: str
    node_id: str
    start: int
    inp: Any
    workflow_id: str
    task_queue: str
    end: int | None = None
    outcome: str = ""  # ok、error、cancelled


def base_id(node_id: str) -> str:
    return node_id.split(".", 1)[0]


async def echo(inp: NodeInput, _: Script) -> dict[str, Any]:
    """默认的执行结果：回显 Node id 与收到的输入，另带一段不该往下传的 rationale。"""
    node = inp.node
    return {"result": {"node": node["id"], "inputs": node.get("inputs", {})}, "rationale": f"RATIONALE-{node['id']}", "meta": {}}


def returns(result: Any) -> Behavior:
    async def behave(inp: NodeInput, _: Script) -> dict[str, Any]:
        return {"result": copy.deepcopy(result), "rationale": f"RATIONALE-{inp.node['id']}"}

    return behave


def sequence(*results: Any) -> Behavior:
    """依次返回，最后一个一直重复。"""
    it = iter(results)
    last: list[Any] = []

    async def behave(inp: NodeInput, _: Script) -> dict[str, Any]:
        value = next(it, None)
        if value is None:
            value = last[0]
        else:
            last[:] = [value]
        return {"result": copy.deepcopy(value), "rationale": f"RATIONALE-{inp.node['id']}"}

    return behave


def slow(seconds: float, inner: Behavior = echo) -> Behavior:
    async def behave(inp: NodeInput, script: Script) -> dict[str, Any]:
        await asyncio.sleep(seconds)
        return await inner(inp, script)

    return behave


def fails(message: str = "执行失败", error_type: str = "stub_failure") -> Behavior:
    async def behave(inp: NodeInput, _: Script) -> dict[str, Any]:
        raise ApplicationError(message, type=error_type, non_retryable=True)

    return behave


def barrier(group: str, size: int, inner: Behavior = echo, timeout: float = 10) -> Behavior:
    """同组的 size 个 Node 都开始之后才一起返回；没有并行时等不齐，按失败处理。"""

    async def behave(inp: NodeInput, script: Script) -> dict[str, Any]:
        arrived = script.barriers.setdefault(group, [0, asyncio.Event()])
        arrived[0] += 1
        if arrived[0] >= size:
            arrived[1].set()
        try:
            await asyncio.wait_for(arrived[1].wait(), timeout)
        except TimeoutError:
            raise ApplicationError(f"{group} 没有并行执行", type="not_parallel", non_retryable=True) from None
        return await inner(inp, script)

    return behave


def blocks_forever() -> Behavior:
    async def behave(inp: NodeInput, _: Script) -> dict[str, Any]:
        while True:
            activity.heartbeat()
            await asyncio.sleep(0.1)

    return behave


@dataclass
class Script:
    plans: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    deliver: dict[str, str] = field(default_factory=dict)
    policy: StagePolicy = field(default_factory=StagePolicy)
    templates: dict[str, TemplateInfo] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    recipes: dict[str, TemplateExpansion] = field(default_factory=dict)
    # Node id、Node id 的顶层部分或 template → 执行行为
    behaviors: dict[str, Behavior] = field(default_factory=dict)
    # Goal id → 第 i 步（从 1 开始）的决定
    tool_steps: dict[str, Callable[[ToolLoopStepInput], ToolLoopStep]] = field(default_factory=dict)
    # 工具名 → 结果；返回 Exception 实例表示工具报错
    tool_results: dict[str, Callable[[ToolCallInput], Any]] = field(default_factory=dict)
    # (挂载点, Node id 或 "stage") → 替换第一个 Hook 结果的字段
    hook_overrides: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # 发问时就回复（模拟秒回）：在 ask_human activity 返回之前调用
    on_question: Callable[[HumanQuestion], Awaitable[None]] | None = None
    # S-10：给了就由它给出判定（ReviewRoundOutput 或旧格式的 Verdict），否则按 plans 与 deliver 判定
    reviewer: Callable[[ReviewRoundInput], Awaitable[Any]] | None = None
    # S-10：给了就按规划请求生成 Stage，否则按 plans 的第 round_seq 个
    plan_for: Callable[[PlanRoundInput], dict[str, Any]] | None = None
    # S-10：收尾写回按这个消息失败（不可重试）
    finalize_error: str = ""
    # Context Builder：Planner 给出的 Task Plan（每轮都给这一份）
    task_plan: dict[str, Any] | None = None
    # T-09：给了就由真的 Policy Engine 执行 Hook（hook_overrides 仍可替换它的第一个结果）
    engine: Any = None
    # T-09：Catalog 来自 Registry（Stage Runner 据此核对 Atomic Node 的 executor、effect）；这时没登记的模板不在结果里
    catalog_authoritative: bool = False
    # T-09：resolve_catalog、load_stage_policy 按这个消息失败（不可重试）
    catalog_error: str = ""
    policy_error: str = ""
    policy_requests: list[StagePolicyRequest] = field(default_factory=list)

    calls: list[Call] = field(default_factory=list)
    hook_calls: list[HookCall] = field(default_factory=list)
    plan_inputs: list[PlanRoundInput] = field(default_factory=list)
    reviews: list[ReviewRoundInput] = field(default_factory=list)
    finalized: list[FinalizeTaskInput] = field(default_factory=list)
    catalog_requests: list[CatalogRequest] = field(default_factory=list)
    scenario_of: dict[str, str] = field(default_factory=dict)
    questions: list[HumanQuestion] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    barriers: dict[str, list[Any]] = field(default_factory=dict)
    invalid_nodes: list[str] = field(default_factory=list)
    running: int = 0
    max_running: int = 0
    _clock: Any = field(default_factory=itertools.count)

    def tick(self) -> int:
        return next(self._clock)

    # --- 查询记录 ---

    def executed(self, task_or_workflow: str | None = None) -> list[Call]:
        return [
            c
            for c in self.calls
            if c.activity in (names.EXECUTE_MODEL_NODE, names.EXECUTE_PROGRAM_NODE)
            and (task_or_workflow is None or c.workflow_id.startswith(task_or_workflow))
        ]

    def call(self, node_id: str) -> Call:
        found = [c for c in self.calls if c.node_id == node_id]
        assert len(found) == 1, f"{node_id} 应恰好执行一次，实际 {[(c.activity, c.node_id) for c in found]}"
        return found[0]

    def review(self, task_id: str, round_seq: int) -> ReviewRoundInput:
        return next(r for r in self.reviews if r.task_id == task_id and r.round_seq == round_seq)

    # --- 执行器 ---

    def _behavior(self, node: dict[str, Any]) -> Behavior:
        # 先按完整 id，再按 template（judge、scorer 等展开出的节点），最后按顶层 id（生成的副本）。
        for key in (node["id"], node.get("template", ""), base_id(node["id"])):
            if key in self.behaviors:
                return self.behaviors[key]
        return echo

    async def _execute(self, name: str, inp: NodeInput) -> dict[str, Any]:
        info = activity.info()
        errors = list(validator("atomic_node").iter_errors(inp.node))
        if errors:
            self.invalid_nodes.append(f"{inp.node.get('id')}: {[e.message for e in errors]}")
        call = Call(name, inp.node["id"], self.tick(), inp, info.workflow_id, info.task_queue)
        self.calls.append(call)
        self.running += 1
        self.max_running = max(self.max_running, self.running)

        async def beat() -> None:
            # 像真实的执行器一样持续发心跳，等待中的 Node 不会因 heartbeat 超时被重试。
            while True:
                activity.heartbeat()
                await asyncio.sleep(0.2)

        beating = asyncio.create_task(beat())
        try:
            out = await self._behavior(inp.node)(inp, self)
            call.outcome = "ok"
            return out
        except asyncio.CancelledError:
            call.outcome = "cancelled"
            raise
        except Exception:
            call.outcome = "error"
            raise
        finally:
            beating.cancel()
            self.running -= 1
            call.end = self.tick()

    @activity.defn(name=names.EXECUTE_MODEL_NODE)
    async def execute_model_node(self, inp: NodeInput) -> dict[str, Any]:
        return await self._execute(names.EXECUTE_MODEL_NODE, inp)

    @activity.defn(name=names.EXECUTE_PROGRAM_NODE)
    async def execute_program_node(self, inp: NodeInput) -> dict[str, Any]:
        return await self._execute(names.EXECUTE_PROGRAM_NODE, inp)

    # --- 决策 ---

    @activity.defn(name=names.PLAN_ROUND)
    async def plan_round(self, inp: PlanRoundInput) -> PlanRoundOutput:
        scenario = str(inp.inputs.get("scenario") or "main")
        self.scenario_of[inp.task_id] = scenario
        self.plan_inputs.append(inp)
        plan = copy.deepcopy(self.task_plan)
        if self.plan_for is not None:
            return PlanRoundOutput(stage=self.plan_for(inp), triage_level="full", task_plan=plan)
        return PlanRoundOutput(stage=copy.deepcopy(self.plans[scenario][inp.round_seq - 1]), triage_level="full", task_plan=plan)

    @activity.defn(name=names.REVIEW_ROUND)
    async def review_round(self, inp: ReviewRoundInput) -> Any:
        self.reviews.append(inp)
        if self.reviewer is not None:
            return await self.reviewer(inp)
        scenario = self.scenario_of[inp.task_id]
        if inp.round_seq < len(self.plans[scenario]):
            return {"verdict": "continue", "reason": "脚本：进入下一轮"}
        deliver = self.deliver.get(scenario, "")
        result = inp.stage_results.get(deliver)
        if result is not None and result.status == "succeeded":
            return {"verdict": "done", "reason": "脚本：完成", "done": {"deliver_node": deliver}}
        return {"verdict": "abort", "reason": "脚本：交付节点没有成功", "abort": {"completed": [], "not_completed": [deliver or "交付"]}}

    @activity.defn(name=names.FINALIZE_TASK)
    async def finalize_task(self, inp: FinalizeTaskInput) -> None:
        self.finalized.append(inp)
        if self.finalize_error:
            raise ApplicationError(self.finalize_error, type="write_back_failed", non_retryable=True)

    @activity.defn(name=names.LOAD_STAGE_POLICY)
    async def load_stage_policy(self, inp: StagePolicyRequest) -> StagePolicy:
        self.policy_requests.append(inp)
        if self.policy_error:
            raise ApplicationError(self.policy_error, type="tenant_policy_not_found", non_retryable=True)
        return self.policy

    @activity.defn(name=names.RESOLVE_CATALOG)
    async def resolve_catalog(self, req: CatalogRequest) -> CatalogInfo:
        self.catalog_requests.append(req)
        if self.catalog_error:
            raise ApplicationError(self.catalog_error, type="template_not_found", non_retryable=True)
        if self.catalog_authoritative:
            templates = {t: self.templates[t] for t in req.templates if t in self.templates}
        else:
            templates = {t: self.templates.get(t, TemplateInfo(executor="program", effect="none")) for t in req.templates}
        return CatalogInfo(templates=templates, tools={t: self.tools.get(t, "read") for t in req.tools}, authoritative=self.catalog_authoritative)

    @activity.defn(name=names.EXPAND_TEMPLATE)
    async def expand_template(self, req: TemplateExpansionRequest) -> TemplateExpansion:
        self.calls.append(Call(names.EXPAND_TEMPLATE, req.node["id"], self.tick(), req, activity.info().workflow_id, activity.info().task_queue))
        return copy.deepcopy(self.recipes[req.node["recipe"]])

    # --- tool_loop ---

    @activity.defn(name=names.TOOL_LOOP_STEP)
    async def tool_loop_step(self, inp: ToolLoopStepInput) -> ToolLoopStep:
        info = activity.info()
        self.calls.append(Call(names.TOOL_LOOP_STEP, inp.step_id, self.tick(), inp, info.workflow_id, info.task_queue, outcome="ok"))
        step = self.tool_steps.get(inp.node["id"]) or self.tool_steps[base_id(inp.node["id"])]
        return step(inp)

    @activity.defn(name=names.CALL_TOOL)
    async def call_tool(self, inp: ToolCallInput) -> Any:
        info = activity.info()
        call = Call(names.CALL_TOOL, inp.node_id, self.tick(), inp, info.workflow_id, info.task_queue)
        self.calls.append(call)
        produce = self.tool_results.get(inp.tool, lambda i: {"tool": i.tool, "args": i.args})
        value = produce(inp)
        if isinstance(value, Exception):
            call.outcome = "error"
            raise ApplicationError(str(value), type="tool_error", non_retryable=True)
        call.outcome = "ok"
        return value

    # --- human 与对账（真实实现见 NodeExecutor） ---

    @activity.defn(name=names.ASK_HUMAN)
    async def ask_human(self, q: HumanQuestion) -> None:
        self.questions.append(q)
        if self.on_question is not None:
            # 在发问的 activity 还没返回时就回复：回复信号先于 activity 结果到达工作流。
            await self.on_question(q)

    @activity.defn(name=names.CANCEL_HUMAN)
    async def cancel_human(self, c: HumanCancel) -> None:
        self.withdrawn.append(c.question_id)

    @activity.defn(name=names.RECONCILE_NODE)
    async def reconcile_node(self, inp: NodeInput) -> ReconcileResult:
        return ReconcileResult(found=False, reason="脚本不对账")

    # --- Policy Engine（local activity） ---

    @activity.defn(name=names.RUN_HOOKS)
    async def run_hooks(self, call: HookCall) -> list[dict[str, Any]]:
        self.hook_calls.append(call)
        subject = call.subject.get("node", {}).get("id", "stage")
        if self.engine is not None:
            results = list(await self.engine.run_hooks(call))
        else:
            results = [{"decision": "allow", "hook": h, "mount": call.mount} for h in call.hooks]
        # (挂载点, Node id 或 "stage")，或 (挂载点, "*") 匹配这个挂载点上的任何一次调用。
        override = self.hook_overrides.get((call.mount, subject)) or self.hook_overrides.get((call.mount, "*"))
        if override is not None and results:
            results[0] = {"hook": results[0]["hook"], "mount": call.mount, **override}
        return results

    def by_queue(self) -> dict[str, list[Any]]:
        return {
            names.DECISION_QUEUE: [
                self.plan_round,
                self.review_round,
                self.finalize_task,
                self.load_stage_policy,
                self.resolve_catalog,
                self.expand_template,
                self.ask_human,
                self.cancel_human,
            ],
            names.MODEL_QUEUE: [self.execute_model_node, self.tool_loop_step],
            names.PROGRAM_QUEUE: [self.execute_program_node, self.call_tool, self.reconcile_node],
        }


SESSION_QUEUE = "stage-session"


@workflow.defn(name=names.SESSION_WORKFLOW, sandboxed=False)
class StandInSession:
    """Go 的 Session 工作流的替身：把 Task 作为子工作流启动，返回收到的第一个 task_finished（S-10 的收尾顺序要用）。"""

    def __init__(self) -> None:
        self._finished: list[task_pb2.TaskWorkflowResult] = []

    @workflow.run
    async def run(self, task: task_pb2.TaskWorkflowInput) -> task_pb2.TaskWorkflowResult:
        await workflow.start_child_workflow(
            names.TASK_WORKFLOW, task, id=task.task_id, task_queue=names.TASK_QUEUE, parent_close_policy=workflow.ParentClosePolicy.ABANDON
        )
        await workflow.wait_condition(lambda: bool(self._finished), timeout=timedelta(seconds=60))
        return self._finished[0]

    @workflow.signal(name=names.TASK_FINISHED_SIGNAL)
    def task_finished(self, result: task_pb2.TaskWorkflowResult) -> None:
        self._finished.append(result)


class StageHarness:
    def __init__(self, client: Client, script: Script) -> None:
        self.client = client
        self.script = script
        self.handles: list[WorkflowHandle[Any, Any]] = []

    async def start(self, *, scenario: str = "main", **kwargs: Any) -> tuple[task_pb2.TaskWorkflowInput, WorkflowHandle[Any, Any]]:
        inp = task_input(scenario=scenario, **kwargs)
        handle = await self.client.start_workflow(
            names.TASK_WORKFLOW, inp, id=inp.task_id, task_queue=names.TASK_QUEUE, result_type=task_pb2.TaskWorkflowResult
        )
        self.handles.append(handle)
        return inp, handle

    async def run(self, *, scenario: str = "main", **kwargs: Any) -> tuple[task_pb2.TaskWorkflowInput, task_pb2.TaskWorkflowResult]:
        inp, handle = await self.start(scenario=scenario, **kwargs)
        return inp, await asyncio.wait_for(handle.result(), timeout=60)

    async def start_via_session(self, *, scenario: str = "main", **kwargs: Any) -> tuple[task_pb2.TaskWorkflowInput, WorkflowHandle[Any, Any]]:
        """经 Session 替身启动：Task 是它的子工作流，收尾时给它发 task_finished。返回 Session 替身的句柄。"""
        inp = task_input(scenario=scenario, **kwargs)
        session = await self.client.start_workflow(
            names.SESSION_WORKFLOW, inp, id=f"session-for-{inp.task_id}", task_queue=SESSION_QUEUE, result_type=task_pb2.TaskWorkflowResult
        )
        self.handles.extend([session, self.client.get_workflow_handle(inp.task_id)])
        return inp, session

    async def wait_for(self, predicate: Callable[[], bool], what: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError(f"{timeout} 秒内没有等到：{what}")
            await asyncio.sleep(0.05)

    async def terminate_all(self) -> None:
        for handle in self.handles:
            with contextlib.suppress(Exception):
                await handle.terminate("测试结束")


@pytest_asyncio.fixture(loop_scope="session")
async def harness(temporal_env: WorkflowEnvironment) -> AsyncIterator[StageHarness]:
    script = Script()
    client = temporal_env.client
    workers = [
        Worker(client, task_queue=SESSION_QUEUE, workflows=[StandInSession]),
        Worker(client, task_queue=names.TASK_QUEUE, workflows=[TaskWorkflow, SubAgentWorkflow, SubgraphWorkflow], activities=[script.run_hooks]),
        *(Worker(client, task_queue=q, activities=acts) for q, acts in script.by_queue().items()),
    ]
    h = StageHarness(client, script)
    async with contextlib.AsyncExitStack() as stack:
        for w in workers:
            await stack.enter_async_context(w)
        try:
            yield h
        finally:
            await h.terminate_all()
    assert not script.invalid_nodes, "交给执行器的 Node 都必须是合法的 Atomic Node：\n" + "\n".join(script.invalid_nodes)


def dumps(value: Any) -> str:
    def default(o: Any) -> Any:
        if hasattr(o, "__dataclass_fields__"):
            return {k: getattr(o, k) for k in o.__dataclass_fields__}
        return str(o)

    return json.dumps(value, ensure_ascii=False, default=default)
