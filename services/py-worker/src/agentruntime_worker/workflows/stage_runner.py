"""Stage Runner（设计书 3.2.3）：在 Task 工作流里把一个 Stage DAG 跑完。

1. 调度：依赖满足的 Node 进入 ready，按 Node id 排序后启动；同时执行的叶子 Node 不超过 StagePolicy.max_parallel，
   超过的排队；会写的 Node（effect 为 write_*）在工作流内串行，不用分布式锁。上游失败的 Node 记为 skipped。
2. Hook：Stage 前后、每个 Node 前后由 Policy Engine 执行 Hook（local activity run_hooks）；
   write_irreversible 的 Node 前自动插入人工审批 gate，tool_loop 里每次调用不可逆工具前也逐次审批（D16）。
   deny 记为 denied，交给 Reviewer；modify 改写输入或输出。
3. quality：revise、ensemble、search 展开为子图，子 Node id 为“父 id + 序号”（见 stage_plan）。
4. Goal Node：template 展开为 Recipe 的子图，大批量拆到 SubgraphWorkflow 子工作流；tool_loop 每一步是一个 activity；
   sub_agent 在 SubAgentWorkflow 子工作流里跑独立上下文、只交回结果，默认只读；sub_task 嵌套一个完整的 Task 工作流，
   深度不超过 2。
5. 汇集：结果按 Node id 排序后合并；下游只拿 result，rationale 不往下传。
6. 进度事件（T-11b）：在顶层 Task 里，Stage 的顶层 Node 结束时推 node_finished，发出人工问题后推 human_input_required。
7. TMP 计划（eval-observability-and-runtime-fixes）第 2～4 节：tool_loop 每一步之前查时间预算、每次工具调用之前查调用次数，
   用完就停这个节点（error_code budget_exhausted）；列工作区根得到空目录时结果带硬信号 environment；tool_loop 的一步由工作流
   重试（workflows/decisions.py）。工具调用次数（含子工作流里的）记在 tool_calls，Task 工作流据此记用量。

叶子 Node（不挂 quality 的 model、program 节点）一开始就排队拿执行名额，名额按 Node 启动的先后发放，所以执行顺序
与 Node id 的顺序一致；Hook 在名额内执行。组合 Node（quality、Goal）不占名额，由它们展开出的叶子去拿，不会死锁。
写节点先拿写锁再拿名额；组合 Node 若会写，整个展开期间持有写锁，子 Node 不再重复去拿。

工作流代码必须确定（CLAUDE.md）。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Protocol

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, FailureError, TimeoutError

with workflow.unsafe.imports_passed_through():
    from google.protobuf import json_format, struct_pb2

    from agentruntime.common.v1 import enums_pb2
    from agentruntime.workflow.v1 import task_pb2

    from agentruntime_worker import names
    from agentruntime_worker.executor import keys
    from agentruntime_worker.activities.types import (
        CatalogInfo,
        CatalogRequest,
        GateRecord,
        HookCall,
        HumanCancel,
        HumanQuestion,
        NodeInput,
        ReconcileResult,
        StagePolicy,
        StagePolicyRequest,
        StageResult,
        TemplateExpansion,
        TemplateExpansionRequest,
        TemplateInfo,
        Tenant,
        ToolCallInput,
        ToolLoopStep,
        ToolLoopStepInput,
    )
    from agentruntime_worker.workflows import decisions, patches
    from agentruntime_worker.workflows import stage_plan as sp
    from agentruntime_worker.workflows import timeouts
    from agentruntime_worker.workflows.events import EventPublisher, sandbox_startup_ms, tool_target
    from agentruntime_worker.workflows.failures import caused_by_cancel, failure_class, failure_message
    from agentruntime_worker.workflows.patches import (
        BUDGETS_PATCH,
        DECISION_RETRY_PATCH,
        HUMAN_CHANNEL_PATCH,
        HUMAN_REPLY_RACE_PATCH,
        POLICY_PATCH,
        R01_FIXES_PATCH,
        RECONCILE_PATCH,
        TOOL_LOOP_APPROVAL_PATCH,
    )

# memo 里记下所属租户，与 Session 工作流启动 Task 时一致（libs/go-core adapters/temporal）。
SCOPE_MEMO_KEY = "agentruntime_scope"
# tool_loop 没写 max_iterations 时的上限。
DEFAULT_MAX_ITERATIONS = 20
# 同一个 Goal Node 里不可逆工具调用被拒（含超时）几次就记为 denied，交给 Reviewer。
MAX_TOOL_REJECTIONS = 2
DEFAULT_ROLE = "transform"
# 审批问题里写进去的输入（规范化 JSON）最多这么多字符（R-01 评审第 4 节第 2 条）。
APPROVAL_INPUTS_LIMIT = 2000
# 列这些路径就是列工作区根（TMP 计划第 2 节的 environment 信号）。
WORKSPACE_ROOTS = ("", ".", "./", "/")


def _minutes(seconds: float) -> float:
    return round(seconds / 60, 2)


def _lists_empty_root(tool: str, args: Any, observation: Any) -> bool:
    """repo_list 列工作区根得到空目录：工作区本身是空的（TMP 计划第 2 节）。"""
    if tool != "repo_list" or not isinstance(observation, dict) or observation.get("entries") != []:
        return False
    path = args.get("path") if isinstance(args, dict) else None
    return path is None or (isinstance(path, str) and path.strip() in WORKSPACE_ROOTS)


def effect_name(value: int) -> str:
    """TaskWorkflowInput.effect_ceiling 的名字；没填时按只读处理。"""
    if not value:
        return "read"
    return enums_pb2.Effect.Name(value).removeprefix("EFFECT_").lower()


def _memo(tenant: Tenant) -> dict[str, Any]:
    return {SCOPE_MEMO_KEY: tenant.tenant_id}


def _merged(*groups: list[str]) -> list[str]:
    """按出现的先后合并几组 Hook 名，同名的只留第一个。"""
    out: list[str] = []
    for group in groups:
        for name in group:
            if name not in out:
                out.append(name)
    return out


def _role(node: dict[str, Any]) -> str:
    return str(node.get("role") or DEFAULT_ROLE)


def _denied(role: str, reason: str, status: str = "failed") -> StageResult:
    return StageResult(role=role, status=status, failure_class="denied", error=reason)


def _failed(role: str, error: str, cls: str = "") -> StageResult:
    return StageResult(role=role, status="failed", failure_class=cls, error=error)


def _as(result: StageResult, role: str, **changes: Any) -> StageResult:
    return replace(result, role=role, **changes)


def _max_effect(nodes: list[dict[str, Any]], ceiling: str) -> str:
    effects = [sp.node_effect(n, ceiling) for n in nodes] or ["none"]
    return max(effects, key=sp.effect_rank)


def _copy_for(node: dict[str, Any], new_id: str, extra_inputs: dict[str, Any]) -> dict[str, Any]:
    """quality 的一次生成：原 Node 的副本，换 id，去掉 quality 与 after，输入加上反馈等。"""
    copied = {k: copy.deepcopy(v) for k, v in node.items() if k not in ("quality", "after")}
    copied["id"] = new_id
    copied["inputs"] = {**copy.deepcopy(node.get("inputs", {})), **extra_inputs}
    return copied


class HumanGateway(Protocol):
    """等人工回复：Task 工作流收到 human_reply 信号后放行。只接受 options 里的选项；超过 timeout 返回 None。"""

    def expect_reply(self, question_id: str, options: tuple[str, ...]) -> None:
        """问题发出之前先登记要等的回复：问题刚发出、工作流还没开始等时到达的回复也要收下。"""

    def forget_reply(self, question_id: str) -> None:
        """问题没发出去时撤销登记。"""

    async def wait_for_reply(
        self, question_id: str, options: tuple[str, ...], timeout: timedelta | None = None
    ) -> task_pb2.HumanReplySignal | None: ...


def _timed_out(err: BaseException) -> bool:
    cause: BaseException | None = err
    while cause is not None:
        if isinstance(cause, TimeoutError):
            return True
        cause = cause.cause if isinstance(cause, FailureError) else None
    return False


def _error_type_is(err: BaseException, error_type: str) -> bool:
    cause: BaseException | None = err
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type == error_type:
            return True
        cause = cause.cause if isinstance(cause, FailureError) else None
    return False


def _unknown(err: BaseException) -> bool:
    """Node Executor 报告写操作结果未知（ApplicationError 的 type 为 unknown）。"""
    return _error_type_is(err, "unknown")


def _needs_reconcile(err: BaseException) -> bool:
    """Node Executor 报告写操作结果未知、这次没能查询（type 为 reconcile），要工作流接着对账。"""
    return _error_type_is(err, "reconcile")


@dataclass
class RunContext:
    """Stage Runner 跑一个 Stage 需要的东西。policy 在一个 Task 内只加载一次。"""

    tenant: Tenant
    task_id: str
    round_seq: int
    effect_ceiling: str
    task_state: dict[str, Any] = field(default_factory=dict)
    pinned: dict[str, Any] = field(default_factory=dict)
    depth: int = 0
    policy: StagePolicy | None = None
    # 只有 Task 工作流里有；sub_task 据此构造子 Task 的输入。
    task_input: task_pb2.TaskWorkflowInput | None = None
    # 当前的 Task Plan；tool_loop 的每一步带上它与 Task State，给 Context Builder 用（只是输入）。
    task_plan: dict[str, Any] | None = None
    # T-09（POLICY_PATCH 之后的运行为 True）：策略声明的 Hook、Hook 的 subject 带预算与写者信息、按 Registry 核对 effect。
    t09: bool = False
    # Task 的预算（美元，没有为 None）与这个 Stage 开始之前已花的钱（之前各 Round 加上这一轮的规划）：budget_check 用。
    budget_usd: float | None = None
    spent_before_usd: float = 0.0
    # R01_FIXES_PATCH 之后的运行为 True：没成功的 Node 也审计；tool_loop 每一步之前查预算。
    r01: bool = False
    # T-11b：只有顶层 Task 工作流的新运行有；顶层 Node 结束与发出人工问题时推送进度事件。子工作流里为 None。
    events: EventPublisher | None = None
    # TMP 计划第 3 节（BUDGETS_PATCH 之后的运行为 True）：工具调用次数的上限（0 不限）与这个 Stage 开始之前已发出的；
    # 时间预算（秒，0 不限）与已用了多少秒（只算 Agent 在跑的时间，由 Task 工作流给出）。
    budgets: bool = False
    tool_calls_limit: int = 0
    tool_calls_before: int = 0
    wall_clock_limit_s: float = 0.0
    active_s: Callable[[], float] | None = None
    # 同一计划第 4 节（DECISION_RETRY_PATCH 之后的运行为 True）：tool_loop 的一步由工作流重试。
    decision_retry: bool = False


@dataclass
class _Env:
    """一个 DAG 的执行环境：已完成的结果（供 $ 引用）、Goal 的输入、是否已经持有写锁。"""

    results: dict[str, StageResult]
    goal_inputs: dict[str, Any] | None = None
    write_held: bool = False

    def view(self) -> dict[str, Any]:
        return {k: r.result for k, r in self.results.items() if r.status == "succeeded"}


@dataclass
class _HookOutcome:
    denied: str = ""
    input_patches: list[dict[str, Any]] = field(default_factory=list)
    output_patches: list[dict[str, Any]] = field(default_factory=list)
    injections: list[dict[str, Any]] = field(default_factory=list)


class StageRunner:
    def __init__(self, ctx: RunContext, human: HumanGateway | None) -> None:
        self._ctx = ctx
        self._human = human
        self._catalog = CatalogInfo()
        self._slots: asyncio.Semaphore | None = None
        self._write_lock = asyncio.Lock()
        # S-10：重跑时给指定 Node 的注入（错误与 Reviewer 的指导）；本次运行里各次执行花的钱（Round 的成本）。
        self._extra_injections: dict[str, list[dict[str, Any]]] = {}
        self.spent_usd = 0.0
        # TMP 计划第 3 节：本次运行发出的工具调用（含子工作流里的）。
        self.tool_calls = 0
        # T-09：本 Stage 开始时 Catalog 取不到的原因（这时整个 Stage 拒绝）；已经问过 Catalog 的模板（没登记的不再问）。
        self._catalog_error = ""
        self._asked_templates: set[str] = set()
        # T-11b：顶层 DAG 的执行环境：它里面的 Node 结束时推送 node_finished。
        self._top: _Env | None = None

    @property
    def policy(self) -> StagePolicy:
        assert self._ctx.policy is not None
        return self._ctx.policy

    # --- Stage ---

    async def run(
        self,
        stage: dict[str, Any],
        *,
        carried: dict[str, StageResult] | None = None,
        injections: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, StageResult]:
        """跑完一个 Stage，返回顶层 Node 的结果，按 Node id 排序。展开出的子 Node 的结果只进 trace。

        重跑（S-10）时 carried 是沿用的结果：这些 Node 不再执行，结果照样供下游引用并一起返回；
        injections 是给指定 Node 的额外注入。只有新的 Verdict 去向会传这两项，之前的历史不受影响。
        """
        carried = dict(carried or {})
        self._extra_injections = {k: list(v) for k, v in (injections or {}).items()}
        await self._prepare(stage)
        nodes = list(stage.get("nodes", []))
        errors = sp.validate_stage(stage, max_nodes=self.policy.max_inline_nodes)
        if errors:
            reason = "invalid_stage：" + "；".join(errors)
            return {n["id"]: StageResult(role=_role(n), status="skipped", error=reason) for n in sorted(nodes, key=lambda n: n["id"])}
        if self._catalog_error:
            # T-09：Node Catalog 取不到时整个 Stage 拒绝，一个 Node 也不执行（fail-closed）。
            reason = f"catalog_unavailable：Node Catalog 取不到（{self._catalog_error}）"
            return {n["id"]: _denied(_role(n), reason) for n in sorted(nodes, key=lambda n: n["id"])}
        hooks = stage.get("hooks") or {}
        subject: dict[str, Any] = {"stage": {k: stage.get(k) for k in ("seq", "goal", "exit_when")}}
        before_hooks, after_hooks = list(hooks.get("before", [])), list(hooks.get("after", []))
        if self._ctx.t09:
            # 策略声明的 Hook 在前，Stage 里写的在后（细化第 2 节第 3 条）。
            subject = {"stage": {k: stage.get(k) for k in ("seq", "goal", "exit_when", "budget")}, "budget": self._budget()}
            before_hooks = _merged(self._policy_hooks("before_stage"), before_hooks)
            after_hooks = _merged(self._policy_hooks("after_stage"), after_hooks)
        before = await self._hooks("before_stage", before_hooks, subject)
        if before.denied:
            return {n["id"]: _denied(_role(n), before.denied, status="skipped") for n in sorted(nodes, key=lambda n: n["id"])}
        kept = {n["id"]: carried[n["id"]] for n in nodes if n["id"] in carried}
        self._top = _Env(results=dict(kept))
        results = await self._run_dag([n for n in nodes if n["id"] not in kept], self._top)
        results = {**kept, **results}
        after = await self._hooks("after_stage", after_hooks, {**subject, "results": {k: v.status for k, v in sorted(results.items())}})
        if after.denied:
            # Stage 的产出被拒绝：结果不再往下交，由 Reviewer 判定 repair 还是 replan。
            results = {k: StageResult(role=v.role, status="failed", failure_class="denied", error=after.denied, quality=v.quality) for k, v in results.items()}
        return dict(sorted(results.items()))

    async def _prepare(self, stage: dict[str, Any]) -> None:
        """Stage 开始前：加载 StagePolicy（每个 Task 一次）与 Node Catalog。两行调换会改变命令顺序，重放测试据此证明自己有效。
        T-09 之后 Catalog 取不到时不再让工作流失败，而是记下原因，整个 Stage 拒绝。"""
        await self._ensure_policy()  # replay-mutation:C
        await self._ensure_stage_catalog(stage)  # replay-mutation:D

    async def _ensure_stage_catalog(self, stage: dict[str, Any]) -> None:
        if not self._ctx.t09:
            await self._ensure_catalog(stage)
            return
        try:
            await self._ensure_catalog(stage)
        except FailureError as err:
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            self._catalog_error = failure_message(err)

    async def _ensure_policy(self) -> None:
        if self._ctx.policy is None:
            self._ctx.policy = await workflow.execute_activity(
                names.LOAD_STAGE_POLICY,
                StagePolicyRequest(tenant=self._ctx.tenant, task_id=self._ctx.task_id, pinned=self._ctx.pinned),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=timeouts.DECISION_RETRY,
                result_type=StagePolicy,
            )
        if self._slots is None:
            self._slots = asyncio.Semaphore(max(1, self.policy.max_parallel))

    async def _ensure_catalog(self, stage: dict[str, Any]) -> None:
        await self._ensure_catalog_for(list(stage.get("nodes", [])))

    async def _ensure_catalog_for(self, nodes: list[dict[str, Any]]) -> None:
        """查出 judge、scorer 模板的执行方式与 Goal Node 工具的 effect；T-09 之后连 Atomic Node 的模板一并查
        （按 Registry 核对 executor、effect）。已经查过的不再查。"""
        templates: list[str] = []
        tools: list[str] = []
        for node in nodes:
            quality = node.get("quality") or {}
            refs = [quality.get(key) for key in ("judge", "scorer")]
            if self._ctx.t09 and node.get("kind") == "atomic":
                refs.append(node.get("template"))
            for ref in refs:
                if ref and ref not in self._catalog.templates and ref not in templates:
                    templates.append(ref)
            if node.get("kind") == "goal":
                for tool in node.get("tools", []):
                    if tool not in self._catalog.tools and tool not in tools:
                        tools.append(tool)
        if not templates and not tools:
            return
        self._asked_templates.update(templates)
        info = await workflow.execute_activity(
            names.RESOLVE_CATALOG,
            CatalogRequest(tenant=self._ctx.tenant, task_id=self._ctx.task_id, templates=sorted(templates), tools=sorted(tools), pinned=self._ctx.pinned),
            task_queue=names.DECISION_QUEUE,
            start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
            retry_policy=timeouts.DECISION_RETRY,
            result_type=CatalogInfo,
        )
        self._catalog.templates.update(info.templates)
        self._catalog.tools.update(info.tools)
        self._catalog.authoritative = self._catalog.authoritative or info.authoritative

    # --- T-09：策略声明的 Hook、Hook 的 subject 与按 Registry 核对 effect ---

    def _policy_hooks(self, mount: str) -> list[str]:
        return list(self.policy.hooks.get(mount, []))

    def _budget(self) -> dict[str, Any]:
        """budget_check 用：Task 的预算与已花的钱（之前各 Round、这一轮的规划，加上本 Stage 到现在）。"""
        return {"task_usd": self._ctx.budget_usd, "spent_usd": round(self._ctx.spent_before_usd + self.spent_usd, 6)}

    def _budget_spent(self, nid: str) -> str:
        """tool_loop 每一步之前：Task 的预算用完了就返回拒绝的理由（规则同 budget_check；纯计算，不产生命令）。"""
        budget = self._budget()
        task_usd, spent = budget["task_usd"], budget["spent_usd"]
        if task_usd is None or spent < task_usd:
            return ""
        return f"budget_check：Task 的预算 {task_usd:g} 美元已经用完（已花 {spent:.4f}），{nid} 不再请求下一步"

    def _budget_stop(self, nid: str, *, before_call: bool) -> StageResult | None:
        """TMP 计划第 3 节：tool_loop 每一步之前查时间、每次工具调用之前查调用次数，用完了返回让这个节点停下的结果
        （failed、denied，error_code budget_exhausted）；没用完或不限时 None。纯计算，不产生命令。"""
        ctx = self._ctx
        if not ctx.budgets:
            return None
        if before_call:
            limit, used = ctx.tool_calls_limit, ctx.tool_calls_before + self.tool_calls
            if limit <= 0 or used < limit:
                return None
            params: dict[str, Any] = {"budget": "tool_calls", "limit": limit, "used": used}
            text = f"工具调用预算 {limit} 次已经用完，{nid} 不再调用工具"
        else:
            if ctx.wall_clock_limit_s <= 0 or ctx.active_s is None:
                return None
            used_s = ctx.active_s()
            if used_s < ctx.wall_clock_limit_s:
                return None
            params = {"budget": "wall_clock", "limit": _minutes(ctx.wall_clock_limit_s), "used": _minutes(used_s)}
            text = f"时间预算 {params['limit']:g} 分钟已经用完（已跑 {params['used']:g} 分钟），{nid} 不再请求下一步"
        return StageResult(role=DEFAULT_ROLE, status="failed", failure_class="denied", error=text, error_code="budget_exhausted", params=params)

    def _node_hooks(self, node: dict[str, Any], ceiling: str) -> dict[str, list[str]]:
        """按 effect 自动插入的默认 Hook，加上策略声明的；策略只能加 Hook，去不掉默认的；audit_log 永远最后。
        after_node 上策略声明的在默认的前面：脱敏一类的改写先作用，trace_result、checkpoint 记的是改过的结果
        （R-01 另一家复审第 4 条：之前 trace_result 先记了原始结果）。"""
        hooks = sp.default_node_hooks(node, ceiling)
        if not self._ctx.t09:
            return hooks
        after = [h for h in hooks["after_node"] if h != "audit_log"]
        return {
            "before_node": _merged(hooks["before_node"], self._policy_hooks("before_node")),
            "after_node": [*_merged([h for h in self._policy_hooks("after_node") if h != "audit_log"], after), "audit_log"],
        }

    async def _registered(self, node: dict[str, Any]) -> str:
        """按 Registry 登记的模板核对 Atomic Node：executor 不同、effect 写低了、模板没登记，返回拒绝的理由；Catalog 不是来自
        Registry（桩）时不核对。拒绝在 before_node Hook 与审批之前，被拒的 Node 不问人、不执行。"""
        template = str(node.get("template", ""))
        if template not in self._catalog.templates and template not in self._asked_templates:
            try:
                await self._ensure_catalog_for([node])
            except FailureError as err:
                if caused_by_cancel(err):
                    raise asyncio.CancelledError() from err
                return f"catalog_unavailable：Node Catalog 取不到（{failure_message(err)}）"
        if not self._catalog.authoritative:
            return ""
        info = self._catalog.templates.get(template)
        if info is None:
            return f"template_not_registered：{node['id']} 的模板 {template} 没有在 Registry 登记"
        if node.get("executor") != info.executor:
            return f"executor_mismatch：{node['id']} 写的 executor 是 {node.get('executor')}，模板 {template} 登记的是 {info.executor}"
        if sp.effect_rank(str(node.get("effect", "none"))) < sp.effect_rank(info.effect):
            return f"effect_mismatch：{node['id']} 写的 effect 是 {node.get('effect', 'none')}，低于模板 {template} 登记的 {info.effect}"
        return ""

    # --- 调度 ---

    def _slot(self) -> asyncio.Semaphore:
        assert self._slots is not None
        return self._slots

    def _writer(self, effect: str, env: _Env) -> Any:
        if effect in sp.WRITE_EFFECTS and not env.write_held:
            return self._write_lock
        return contextlib.nullcontext()

    async def _run_dag(self, nodes: list[dict[str, Any]], env: _Env) -> dict[str, StageResult]:
        """按依赖调度一个 DAG：ready 的 Node 按 id 排序后启动，上游没成功的记为 skipped。"""
        pending = {n["id"]: n for n in nodes}
        done = env.results
        running: dict[str, asyncio.Task[StageResult]] = {}
        try:
            while pending or running:
                progressed = True
                while progressed:
                    progressed = False
                    for nid in sorted(pending):
                        node = pending[nid]
                        deps = list(node.get("after", []))
                        if not all(d in done for d in deps):
                            continue
                        del pending[nid]
                        progressed = True
                        failed_dep = next((d for d in deps if done[d].status != "succeeded"), None)
                        if failed_dep is not None:
                            done[nid] = StageResult(role=_role(node), status="skipped", failure_class="upstream_failed", error=f"上游 {failed_dep} 没有成功")
                            await self._node_finished(env, nid, done[nid])
                        else:
                            await self._node_started(env, node)
                            running[nid] = asyncio.create_task(self._run_node(node, env))
                if not running:
                    break
                await workflow.wait(list(running.values()), return_when=asyncio.FIRST_COMPLETED)
                for nid in sorted(k for k, t in running.items() if t.done()):
                    done[nid] = running.pop(nid).result()
                    await self._node_finished(env, nid, done[nid])
        except asyncio.CancelledError:
            for task in running.values():
                task.cancel()
            if running:
                await workflow.wait(list(running.values()))
            raise
        return {n["id"]: done[n["id"]] for n in nodes if n["id"] in done}

    async def _node_finished(self, env: _Env, node_id: str, result: StageResult) -> None:
        """T-11b：顶层 DAG 里的 Node 结束时推送 node_finished；展开出的子 Node 不推。"""
        if self._ctx.events is not None and env is self._top:
            await self._ctx.events.node_finished(self._ctx.round_seq, node_id, result)

    async def _node_started(self, env: _Env, node: dict[str, Any]) -> None:
        """顶层 DAG 里的 Node 开始执行时推送 node_started（PROGRESS_DETAIL_PATCH 之后的运行）；展开出的子 Node 不推。"""
        if self._ctx.events is not None and env is self._top:
            await self._ctx.events.node_started(self._ctx.round_seq, node)

    async def _gather(self, coros: list[Awaitable[Any]]) -> list[Any]:
        """并发执行，结果按传入的顺序返回；取消时一并取消。"""
        tasks = [asyncio.ensure_future(c) for c in coros]
        return list(await asyncio.gather(*tasks))

    # --- 单个 Node ---

    async def _run_node(self, node: dict[str, Any], env: _Env, *, resolved: bool = False) -> StageResult:
        role = _role(node)
        nid = node["id"]
        ceiling = self._ctx.effect_ceiling
        effect = sp.node_effect(node, ceiling)
        if sp.effect_rank(effect) > sp.effect_rank(ceiling):
            return _denied(role, f"{nid} 的 effect {effect} 超过 Task 允许的 {ceiling}")
        if self._ctx.t09 and node.get("kind") == "atomic":
            reason = await self._registered(node)
            if reason:
                return _denied(role, reason)
        if resolved:
            inputs = node.get("inputs", {})
        else:
            try:
                inputs = sp.resolve_inputs(node.get("inputs", {}), results=env.view(), task_state=self._ctx.task_state, goal_inputs=env.goal_inputs)
            except sp.UnresolvedReference as err:
                return _failed(role, f"unresolved_reference：{err}")
        node = {**node, "inputs": inputs}
        hooks = self._node_hooks(node, ceiling)
        leaf = (
            node.get("kind") == "atomic"
            and sp.quality_type(node) == "none"
            and node.get("executor") in ("model", "program")
            and effect != "write_irreversible"
        )
        if leaf:
            async with self._writer(effect, env), self._slot():
                return await self._guarded(node, env, hooks, role)
        return await self._guarded(node, env, hooks, role)

    async def _guarded(self, node: dict[str, Any], env: _Env, hooks: dict[str, list[str]], role: str) -> StageResult:
        """before_node Hook → 执行 → after_node Hook。"""
        subject: dict[str, Any] = {"node": node}
        if self._ctx.t09:
            # 写者已串行化：会写的 Atomic Node 由 _writer 串行（叶子在这之前已拿到写锁，不可逆写在 _dispatch 里拿），
            # Goal Node 在展开期间持有写锁。幂等键按最终输入由代码生成（_execute），这里先按当前输入算一份给 Hook 核对。
            subject["budget"] = self._budget()
            subject["write_serialized"] = env.write_held or node.get("kind") == "goal" or (
                node.get("executor") in ("model", "program") and sp.quality_type(node) == "none"
            )
            if node.get("kind") == "atomic" and node.get("effect") in sp.WRITE_EFFECTS:
                subject["idempotency_key"] = keys.idempotency_key(self._ctx.tenant.tenant_id, self._ctx.task_id, node["id"], node.get("inputs", {}))
        before = await self._hooks("before_node", hooks["before_node"], subject)
        if before.denied:
            return _denied(role, before.denied)
        if before.input_patches:
            try:
                node = {**node, "inputs": sp.apply_patch(node.get("inputs", {}), before.input_patches)}
            except sp.PatchError as err:
                return _denied(role, f"Hook 改写输入失败：{err}")
        outcome = _as(await self._dispatch(node, env, [*self._extra_injections.get(node["id"], []), *before.injections]), role)
        if outcome.status != "succeeded":
            if self._ctx.r01 and "audit_log" in hooks["after_node"]:
                # R-01 评审第 4 节第 3 条：失败、结果未知、审批被拒的 Node 也审计（只审计，不跑 after_node 的其他 Hook）。
                audit = {"node": node, "result": None, "status": outcome.status, "failure_class": outcome.failure_class}
                await self._hooks("after_node", ["audit_log"], audit)
            return outcome
        after = await self._hooks("after_node", hooks["after_node"], {"node": node, "result": outcome.result})
        if after.denied:
            return _denied(role, after.denied)
        if after.output_patches:
            try:
                outcome = replace(outcome, result=sp.apply_patch(outcome.result, after.output_patches))
            except sp.PatchError as err:
                return _denied(role, f"Hook 改写输出失败：{err}")
        return outcome

    async def _dispatch(self, node: dict[str, Any], env: _Env, injections: list[dict[str, Any]]) -> StageResult:
        role = _role(node)
        if sp.quality_type(node) != "none":
            return await self._run_quality(node, env, role)
        if node.get("kind") == "goal":
            return await self._run_goal(node, env, role)
        executor = node.get("executor")
        if executor == "human":
            return await self._run_human(node, role)
        if node.get("effect") == "write_irreversible":
            gate, reason = await self._approval(node)
            if gate is None:
                return _denied(role, reason)
            async with self._writer("write_irreversible", env), self._slot():
                return await self._execute(node, injections, gate)
        return await self._execute(node, injections, None)

    async def _execute(self, node: dict[str, Any], injections: list[dict[str, Any]], gate: GateRecord | None) -> StageResult:
        role = _role(node)
        model = node.get("executor") == "model"
        effect = str(node.get("effect", "none"))
        if effect in sp.WRITE_EFFECTS:
            # 幂等键一律由代码按 tenant_id、task_id、node_id 与最终的输入生成，Planner 或模型写的不算数（附录 D.4）。
            node = {**node, "idempotency_key": keys.idempotency_key(self._ctx.tenant.tenant_id, self._ctx.task_id, node["id"], node.get("inputs", {}))}
        start_to_close, heartbeat = timeouts.model_node_timeouts((node.get("budget") or {}).get("timeout_s"))
        # write_irreversible 不自动重试（附录 D.4）。
        attempts = 1 if effect == "write_irreversible" else int((node.get("retry") or {}).get("max", 0)) + 1
        try:
            output = await workflow.execute_activity(
                names.EXECUTE_MODEL_NODE if model else names.EXECUTE_PROGRAM_NODE,
                NodeInput(tenant=self._ctx.tenant, task_id=self._ctx.task_id, round_seq=self._ctx.round_seq, node=node, gate=gate, injections=list(injections)),
                task_queue=names.MODEL_QUEUE if model else names.PROGRAM_QUEUE,
                start_to_close_timeout=start_to_close,
                heartbeat_timeout=heartbeat,
                retry_policy=RetryPolicy(initial_interval=timeouts.MIN_HEARTBEAT, maximum_interval=timeouts.MAX_HEARTBEAT, maximum_attempts=attempts),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                result_type=dict,
            )
        except FailureError as err:
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            if _unknown(err):
                return StageResult(role=role, status="unknown", error=failure_message(err))
            # 写操作的 activity 超时，或 Node Executor 查询暂时不可用：外部可能已经生效，结果未知；
            # 先按幂等键对账（reconcile_node 带重试），不当作失败（附录 K.4）。
            if effect in sp.WRITE_EFFECTS and (_timed_out(err) or _needs_reconcile(err)) and workflow.patched(RECONCILE_PATCH):
                return await self._reconcile(node, role)
            return _failed(role, failure_message(err), failure_class(err))
        # 只取 result；rationale 只写入 trace，不往下传（设计书 3.2.4）。成本计入 Round 的成本（S-10）；
        # vendor 是实际服务它的供应商，Reviewer 避开它（附录 H.3）。
        meta = output.get("meta") or {}
        cost = float(meta.get("cost_usd") or 0.0)
        self.spent_usd += cost
        return StageResult(role=role, status="succeeded", result=output.get("result"), cost_usd=cost, vendor=str(meta.get("vendor") or ""))

    async def _reconcile(self, node: dict[str, Any], role: str) -> StageResult:
        try:
            outcome = await workflow.execute_activity(
                names.RECONCILE_NODE,
                NodeInput(tenant=self._ctx.tenant, task_id=self._ctx.task_id, round_seq=self._ctx.round_seq, node=node),
                task_queue=names.PROGRAM_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=timeouts.DECISION_RETRY,
                result_type=ReconcileResult,
            )
        except FailureError as err:
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            return StageResult(role=role, status="unknown", error=f"对账失败，结果未知：{failure_message(err)}")
        if outcome.found:
            return StageResult(role=role, status="succeeded", result=(outcome.output or {}).get("result"))
        return StageResult(role=role, status="unknown", error=outcome.reason or "写操作结果未知")

    async def _ask_and_wait(
        self, question_id: str, text: str, options: tuple[str, ...], timeout_s: int, *, kind: str
    ) -> tuple[task_pb2.HumanReplySignal | None, str]:
        """经 HumanChannel 发出问题，再等 human_reply 信号；等待有持久化定时器，超时或取消时撤回问题。
        返回 (回复, 错误)；超时时回复为 None。S-05 之前的历史没有发问与定时器，按旧路径回放。
        问题发出之后、开始等之前推送 human_input_required（T-11b）；kind 是问题的来源：human、approval、tool_approval。"""
        assert self._human is not None
        if not workflow.patched(HUMAN_CHANNEL_PATCH):
            return await self._human.wait_for_reply(question_id, options), ""
        deadline = workflow.now() + timedelta(seconds=timeout_s)
        # 先登记再发问：信号先于 activity 结果交给工作流，问题刚发出就到的回复不能因为“还没在等”被丢掉。
        # 这改变了早到回复的处理，可能改变命令序列，所以有版本分支；旧历史按旧路径回放。
        early = workflow.patched(HUMAN_REPLY_RACE_PATCH)
        if early:
            self._human.expect_reply(question_id, options)
        try:
            await workflow.execute_activity(
                names.ASK_HUMAN,
                HumanQuestion(
                    tenant=self._ctx.tenant,
                    task_id=self._ctx.task_id,
                    question_id=question_id,
                    text=text,
                    options=list(options),
                    deadline=deadline.isoformat(),
                    round_seq=self._ctx.round_seq,
                ),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=timeouts.DECISION_RETRY,
            )
        except FailureError as err:
            if early:
                self._human.forget_reply(question_id)
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            return None, f"发不出问题：{failure_message(err)}"
        except asyncio.CancelledError:
            if early:
                self._human.forget_reply(question_id)
            raise
        if self._ctx.events is not None:
            await self._ctx.events.human_input_required(self._ctx.round_seq, question_id, kind, text, options, deadline.isoformat())
        try:
            reply = await self._human.wait_for_reply(question_id, options, timedelta(seconds=timeout_s))
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.ensure_future(self._withdraw(question_id)))
            raise
        if reply is None:
            await self._withdraw(question_id)
        return reply, ""

    async def _withdraw(self, question_id: str) -> None:
        with contextlib.suppress(FailureError):
            await workflow.execute_activity(
                names.CANCEL_HUMAN,
                HumanCancel(tenant=self._ctx.tenant, task_id=self._ctx.task_id, question_id=question_id, round_seq=self._ctx.round_seq),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=timeouts.DECISION_RETRY,
            )

    async def _run_human(self, node: dict[str, Any], role: str) -> StageResult:
        """human 节点：问题经 HumanChannel 发出，回复以信号送回；超时按 on_timeout 处理（附录 D.3：拒绝，或带默认值继续）。"""
        if self._human is None:
            return _failed(role, "human 节点只能在 Task 工作流里执行")
        inputs = node.get("inputs") or {}
        options = tuple(str(o) for o in inputs.get("options", ()))
        timeout_s = int(inputs.get("timeout_s") or self.policy.human_timeout_s)
        text = str(inputs.get("question") or f"请处理 {node['id']}")
        reply, error = await self._ask_and_wait(node["id"], text, options, timeout_s, kind="human")
        if error:
            return _failed(role, error)
        if reply is None:
            on_timeout = inputs.get("on_timeout")
            if isinstance(on_timeout, dict) and "default" in on_timeout:
                return StageResult(role=role, status="succeeded", result={"option": str(on_timeout["default"]), "timed_out": True})
            return _denied(role, f"{node['id']} 等人工回复超时（{timeout_s} 秒）")
        return StageResult(role=role, status="succeeded", result={"option": reply.option, "guidance": reply.guidance, "replied_by": reply.replied_by_user_id})

    async def _approval(self, node: dict[str, Any]) -> tuple[GateRecord | None, str]:
        """write_irreversible 的 Node 前自动插入的人工审批（附录 F.3），gate 的 id 是 <id>.1；超时按拒绝处理。"""
        if self._human is None:
            return None, "这里没有人工审批通道，write_irreversible 的 Node 不能执行"
        gate_id = sp.child_id(node["id"], 1)
        timeout_s = self.policy.approval_timeout_s
        # 审批人要看得到写什么（R-01 评审第 4 节第 2 条）：带上要执行的输入，过长截断。
        inputs = json.dumps(node.get("inputs") or {}, ensure_ascii=False, sort_keys=True)
        if len(inputs) > APPROVAL_INPUTS_LIMIT:
            inputs = inputs[:APPROVAL_INPUTS_LIMIT] + "…（截断）"
        text = f"批准执行 {node['id']}（{node.get('template', '')}）吗？输入：{inputs}。这是不可撤销的写操作。"
        reply, error = await self._ask_and_wait(gate_id, text, sp.APPROVAL_OPTIONS, timeout_s, kind="approval")
        if error:
            return None, error
        if reply is None:
            return None, f"人工审批超时（{timeout_s} 秒），按拒绝处理"
        if reply.option != "approve":
            return None, f"人工审批没有通过：{reply.option}"
        return GateRecord(gate_node=gate_id, option=reply.option, approved_by=reply.replied_by_user_id), ""

    async def _hooks(self, mount: str, hooks: list[str], subject: dict[str, Any]) -> _HookOutcome:
        outcome = _HookOutcome()
        if not hooks:
            return outcome
        try:
            results = await workflow.execute_local_activity(
                names.RUN_HOOKS,
                HookCall(tenant=self._ctx.tenant, task_id=self._ctx.task_id, round_seq=self._ctx.round_seq, mount=mount, hooks=hooks, subject=subject),
                start_to_close_timeout=timeouts.HOOK_START_TO_CLOSE,
                retry_policy=timeouts.HOOK_RETRY,
                result_type=list,
            )
        except FailureError as err:
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            # Hook 执行不了时按拒绝处理，不放行。
            outcome.denied = f"Hook 执行失败：{failure_message(err)}"
            return outcome
        for r in results:
            decision = r.get("decision")
            if decision == "deny" and not outcome.denied:
                outcome.denied = f"{r.get('hook')}：{r.get('reason') or '被拒绝'}"
            elif decision not in ("allow", "modify", "inject") and not outcome.denied:
                # 不认识的结果按拒绝处理（fail-closed）；Policy Engine 自己已经这样做，这里兜底。
                outcome.denied = f"{r.get('hook')}：返回了不认识的结果 {decision!r}"
            elif decision == "modify":
                modification = r.get("modification") or {}
                target = outcome.input_patches if modification.get("target") == "input" else outcome.output_patches
                target.extend(modification.get("patch", []))
            elif decision == "inject" and r.get("injection"):
                outcome.injections.append(r["injection"])
        return outcome

    # --- quality ---

    def _aux_node(self, node_id: str, template: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """judge 或 scorer 节点：执行方式取自 Node Catalog。"""
        info = self._catalog.templates.get(template) or TemplateInfo(executor="program", effect="none")
        node: dict[str, Any] = {
            "id": node_id,
            "kind": "atomic",
            "template": template,
            "executor": info.executor,
            "effect": info.effect,
            "role": "judge",
            "inputs": inputs,
        }
        if info.executor == "model":
            node["output"] = info.output or "score"
        return node

    async def _judge(self, node_id: str, template: str, inputs: dict[str, Any], env: _Env) -> StageResult:
        judge = self._aux_node(node_id, template, inputs)
        if judge["effect"] in sp.WRITE_EFFECTS:
            return _denied("judge", f"judge {template} 不能有写操作")
        return await self._run_node(judge, env, resolved=True)

    async def _run_quality(self, node: dict[str, Any], env: _Env, role: str) -> StageResult:
        kind = sp.quality_type(node)
        if kind == "revise":
            return await self._revise(node, env, role)
        if kind == "ensemble":
            return await self._ensemble(node, env, role)
        if kind == "search":
            return await self._search(node, env, role)
        return _failed(role, f"不认识的 quality {kind}")

    async def _revise(self, node: dict[str, Any], env: _Env, role: str) -> StageResult:
        """生成 → judge → 不通过带反馈退回，最多 max_rounds 轮（附录 E.2）。"""
        quality = node["quality"]
        nid = node["id"]
        rounds = int(quality["max_rounds"])
        feedback: Any = None
        last: StageResult | None = None
        for r in range(1, rounds + 1):
            extra = {"feedback": feedback} if feedback is not None else {}
            generated = await self._run_node(_copy_for(node, sp.child_id(nid, 2 * r - 1), extra), env, resolved=True)
            if generated.status != "succeeded":
                return _as(generated, role, quality={"type": "revise", "rounds": r, "passed": False})
            last = generated
            judge_inputs: dict[str, Any] = {"candidate": generated.result}
            if node.get("done_when"):
                judge_inputs["done_when"] = node["done_when"]
            verdict = await self._judge(sp.child_id(nid, 2 * r), quality["judge"], judge_inputs, env)
            if verdict.status != "succeeded":
                return _as(verdict, role, quality={"type": "revise", "rounds": r, "passed": False})
            if isinstance(verdict.result, dict) and verdict.result.get("pass") is True:
                return StageResult(role=role, status="succeeded", result=last.result, quality={"type": "revise", "rounds": r, "passed": True})
            feedback = verdict.result
        assert last is not None
        return StageResult(role=role, status="succeeded", result=last.result, quality={"type": "revise", "rounds": rounds, "passed": False})

    async def _ensemble(self, node: dict[str, Any], env: _Env, role: str) -> StageResult:
        """生成 × N → 投票或 judge 择优（附录 E.2）。"""
        quality = node["quality"]
        nid = node["id"]
        n = int(quality["n"])
        candidates = [_copy_for(node, sp.child_id(nid, i), {"sample_index": i}) for i in range(1, n + 1)]
        outcomes = await self._gather([self._run_node(c, env, resolved=True) for c in candidates])
        succeeded = [(c["id"], o) for c, o in zip(candidates, outcomes, strict=True) if o.status == "succeeded"]
        if not succeeded:
            return _failed(role, f"{n} 个候选都没有成功：{outcomes[0].error}", outcomes[0].failure_class)
        aggregate = quality["aggregate"]
        if aggregate == "vote":
            chosen = succeeded[sp.vote([o.result for _, o in succeeded])]
        else:
            verdict = await self._judge(sp.child_id(nid, n + 1), quality["judge"], {"candidates": [o.result for _, o in succeeded]}, env)
            if verdict.status != "succeeded":
                return _as(verdict, role)
            best = verdict.result.get("best") if isinstance(verdict.result, dict) else None
            if not isinstance(best, int) or not 1 <= best <= len(succeeded):
                return _failed(role, f"judge 给出的 best 不在 1 到 {len(succeeded)} 之间：{best!r}", "invalid_output")
            chosen = succeeded[best - 1]
        return StageResult(
            role=role,
            status="succeeded",
            result=chosen[1].result,
            quality={"type": "ensemble", "n": n, "aggregate": aggregate, "chosen": chosen[0]},
        )

    async def _search(self, node: dict[str, Any], env: _Env, role: str) -> StageResult:
        """每层扩展 k 个候选 → 打分 → 剪枝到 beam_width 个（附录 E.2）。结果取最后一层得分最高的候选。"""
        quality = node["quality"]
        nid = node["id"]
        k, max_depth, beam_width = int(quality["expand_k"]), int(quality["max_depth"]), int(quality["beam_width"])
        seq = 0
        beam: list[Any] = [None]
        chosen: tuple[str, StageResult] | None = None
        for depth in range(1, max_depth + 1):
            candidates: list[dict[str, Any]] = []
            for state in beam:
                for branch in range(1, k + 1):
                    seq += 1
                    candidates.append(_copy_for(node, sp.child_id(nid, seq), {"state": state, "depth": depth, "branch": branch}))
            outcomes = await self._gather([self._run_node(c, env, resolved=True) for c in candidates])
            ok = [(c["id"], o) for c, o in zip(candidates, outcomes, strict=True) if o.status == "succeeded"]
            if not ok:
                return _failed(role, f"第 {depth} 层的候选都没有成功", outcomes[0].failure_class)
            seq += 1
            scored = await self._judge(sp.child_id(nid, seq), quality["scorer"], {"candidates": [o.result for _, o in ok]}, env)
            if scored.status != "succeeded":
                return _as(scored, role)
            scores = scored.result.get("scores") if isinstance(scored.result, dict) else None
            if not isinstance(scores, list) or len(scores) != len(ok):
                return _failed(role, f"scorer 应给出 {len(ok)} 个分数，实际 {scores!r}", "invalid_output")
            kept = sp.prune([float(s) for s in scores], beam_width)
            beam = [ok[i][1].result for i in kept]
            chosen = ok[kept[0]]
        assert chosen is not None
        return StageResult(role=role, status="succeeded", result=chosen[1].result, quality={"type": "search", "depth": max_depth, "chosen": chosen[0]})

    # --- Goal Node ---

    async def _run_goal(self, node: dict[str, Any], env: _Env, role: str) -> StageResult:
        mode = node.get("mode") or ("template" if node.get("recipe") else "tool_loop")
        if mode == "template":
            return await self._template(node, env, role)
        if mode == "tool_loop":
            return await self._inline_tool_loop(node, env, role)
        if mode == "sub_agent":
            return await self._sub_agent(node, role)
        if mode == "sub_task":
            return await self._sub_task(node, env, role)
        return _failed(role, f"不认识的 mode {mode}")

    async def _template(self, node: dict[str, Any], env: _Env, role: str) -> StageResult:
        nid = node["id"]
        try:
            expansion = await workflow.execute_activity(
                names.EXPAND_TEMPLATE,
                TemplateExpansionRequest(tenant=self._ctx.tenant, task_id=self._ctx.task_id, round_seq=self._ctx.round_seq, node=node, pinned=self._ctx.pinned),
                task_queue=names.DECISION_QUEUE,
                start_to_close_timeout=timeouts.DECISION_START_TO_CLOSE,
                retry_policy=timeouts.DECISION_RETRY,
                result_type=TemplateExpansion,
            )
        except FailureError as err:
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            return _failed(role, f"Recipe 展开失败：{failure_message(err)}", failure_class(err))
        limit = self.policy.max_inline_nodes
        goal_inputs = dict(node.get("inputs", {}))
        try:
            if expansion.per_item:
                chains, collect = sp.expand_per_item(nid, expansion.per_item, list(expansion.items), expansion.collect)
                nodes = [n for chain in chains for n in chain]
            else:
                nodes, output = sp.reindex_subgraph(nid, expansion.nodes, expansion.output)
                chains, collect = [], [output]
        except ValueError as err:
            return _failed(role, f"invalid_recipe：{err}")
        if not expansion.per_item and len(nodes) > limit:
            return _denied(role, f"Recipe 展开出 {len(nodes)} 个 Node，超过内联上限 {limit}；大批量请用逐条目展开")
        effect = _max_effect(nodes, self._ctx.effect_ceiling)
        if len(nodes) > limit:
            return await self._template_batches(node, chains, collect, goal_inputs, effect, env, role)
        errors = sp.validate_stage({"nodes": nodes}, max_nodes=limit, top_level=False)
        if errors:
            return _failed(role, "invalid_recipe：" + "；".join(errors))
        await self._ensure_catalog_for(nodes)
        async with self._writer(effect, env):
            done = await self._run_dag(nodes, _Env(results={}, goal_inputs=goal_inputs, write_held=env.write_held or effect in sp.WRITE_EFFECTS))
        if expansion.per_item:
            return _collect(role, [done[c] for c in collect])
        return _as(done[collect[0]], role)

    async def _template_batches(
        self,
        node: dict[str, Any],
        chains: list[list[dict[str, Any]]],
        collect: list[str],
        goal_inputs: dict[str, Any],
        effect: str,
        env: _Env,
        role: str,
    ) -> StageResult:
        """逐条目展开的大批量：按条目切批，每批一个 SubgraphWorkflow 子工作流（计划 S-04 难点“扇出上限”）。"""
        policy = self.policy
        chain_len = max(len(c) for c in chains)
        if chain_len > policy.max_inline_nodes:
            return _denied(role, f"一个条目的链有 {chain_len} 个 Node，超过内联上限 {policy.max_inline_nodes}")
        size = max(1, min(policy.batch_size, policy.max_inline_nodes // chain_len))
        writes = effect in sp.WRITE_EFFECTS
        # 会写的批次一个接一个跑，保证同一时刻只有一个写者。
        limit = asyncio.Semaphore(1 if writes else max(1, policy.batch_parallel))

        async def run_batch(number: int, items: range) -> Any:
            async with limit:
                return await workflow.execute_child_workflow(
                    names.SUBGRAPH_WORKFLOW,
                    SubgraphInput(
                        tenant=self._ctx.tenant,
                        task_id=self._ctx.task_id,
                        round_seq=self._ctx.round_seq,
                        goal_id=node["id"],
                        chains=[chains[i] for i in items],
                        collect=[collect[i] for i in items],
                        task_state=copy.deepcopy(self._ctx.task_state),
                        effect_ceiling=self._ctx.effect_ceiling,
                        policy=policy,
                        goal_inputs=goal_inputs,
                        budget_usd=self._ctx.budget_usd,
                        spent_before_usd=self._budget()["spent_usd"],
                        **self._child_budgets(),
                    ),
                    id=sp.subworkflow_id(self._ctx.task_id, self._ctx.round_seq, node["id"], f"batch-{number}"),
                    task_queue=names.TASK_QUEUE,
                    memo=_memo(self._ctx.tenant),
                    result_type=SubgraphOutput,
                )

        try:
            async with self._writer(effect, env):
                outputs = await self._gather([run_batch(b, r) for b, r in enumerate(sp.batches(len(chains), size), start=1)])
        except FailureError as err:
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            return _failed(role, f"批量子工作流失败：{failure_message(err)}", failure_class(err))
        merged: dict[str, StageResult] = {}
        for out in outputs:
            merged.update(out.results)
            # 子图的花费与工具调用次数带回父 Stage（成本口径的人定方案；TMP 计划第 3 节）。
            self.spent_usd += float(out.cost_usd or 0.0)
            self.tool_calls += int(out.tool_calls or 0)
        return _collect(role, [merged[c] for c in collect])

    async def _check_tools(self, node: dict[str, Any], ceiling: str, *, read_only: bool) -> tuple[dict[str, str] | None, str]:
        """展开时校验 Goal Node 可用工具的 effect 都不高于 ceiling；查不到的工具按最高的 effect 处理。"""
        await self._ensure_catalog_for([node])
        effects = {t: self._catalog.tools.get(t, "write_irreversible") for t in node.get("tools", [])}
        for tool in sorted(effects):
            if sp.effect_rank(effects[tool]) > sp.effect_rank(ceiling):
                if read_only:
                    return None, f"sub_agent 只读，工具 {tool} 的 effect 为 {effects[tool]}"
                return None, f"工具 {tool} 的 effect {effects[tool]} 超过 {node['id']} 允许的 {ceiling}"
        return effects, ""

    async def _inline_tool_loop(self, node: dict[str, Any], env: _Env, role: str) -> StageResult:
        ceiling = str(node.get("effect_ceiling") or self._ctx.effect_ceiling)
        effects, reason = await self._check_tools(node, ceiling, read_only=False)
        if effects is None:
            return _denied(role, reason)
        effect = max(effects.values(), key=sp.effect_rank, default="none")
        # tool_loop 的主 agent 是唯一写者：会写时整个循环持有写锁。
        async with self._writer(effect, env):
            inner = _Env(results={}, write_held=env.write_held or effect in sp.WRITE_EFFECTS)
            # 顶层的 Goal Node 才推每次工具调用的进度（展开出的子 Node、子工作流里都不推）。
            return _as(await self.tool_loop(node, isolated=False, tool_effects=effects, env=inner, progress=env is self._top), role)

    async def tool_loop(
        self, node: dict[str, Any], *, isolated: bool, tool_effects: dict[str, str], env: _Env, progress: bool = False,
    ) -> StageResult:
        """见 _tool_loop。结果另带这次循环发出的工具调用次数与执行中发现的硬信号（TMP 计划第 2、3 节）。"""
        marks: set[str] = set()
        calls_before = self.tool_calls
        result = await self._tool_loop(node, isolated=isolated, tool_effects=tool_effects, env=env, progress=progress, marks=marks)
        return replace(result, signals=sorted(set(result.signals) | marks), tool_calls=self.tool_calls - calls_before)

    async def _tool_loop(
        self, node: dict[str, Any], *, isolated: bool, tool_effects: dict[str, str], env: _Env, progress: bool, marks: set[str],
    ) -> StageResult:
        """思考 → 调工具 → 观察，每一步是一个 activity（附录 E.1）。设最大迭代次数，同一错误连续出现就打断（附录 B.8）。

        调用 effect 为 write_irreversible 的工具前逐次人工审批（D16，附录 E.1）：拒绝或超时时不调用，把结果作为这一步的
        观察送回循环；同一个 Goal Node 被拒 MAX_TOOL_REJECTIONS 次就记为 denied。D16 之前的历史没有审批，按旧路径回放。
        """
        nid = node["id"]
        limit = int(node.get("max_iterations") or DEFAULT_MAX_ITERATIONS)
        history: list[dict[str, Any]] = []
        rejections = 0
        # 每次工具调用前后推 tool_status（只有顶层 Goal Node，且是 PROGRESS_DETAIL_PATCH 之后的运行；见 EventPublisher）。
        events = self._ctx.events if progress else None
        # 这个循环花的钱（每一步的模型花费，计入 Round 的成本）与最近一步的供应商。
        spent_before = self.spent_usd
        vendor = ""
        for i in range(1, limit + 1):
            if self._ctx.r01:
                # 成本口径的人定方案：每一步之前查 Task 的预算，用完就停，交给 Reviewer。
                reason = self._budget_spent(nid)
                if reason:
                    return _denied(DEFAULT_ROLE, reason)
            # TMP 计划第 3 节：时间预算用完就不再请求下一步。
            stop = self._budget_stop(nid, before_call=False)
            if stop is not None:
                return stop
            step_input = ToolLoopStepInput(
                tenant=self._ctx.tenant,
                task_id=self._ctx.task_id,
                round_seq=self._ctx.round_seq,
                node=node,
                iteration=i,
                history=copy.deepcopy(history),
                step_id=sp.child_id(nid, 2 * i - 1),
                isolated=isolated,
                # sub_agent 是独立上下文：不带 Task State 与 Task Plan。
                task_state={} if isolated else copy.deepcopy(self._ctx.task_state),
                task_plan=None if isolated else copy.deepcopy(self._ctx.task_plan),
            )
            try:
                async with self._slot():
                    if self._ctx.decision_retry:
                        # TMP 计划第 4 节：由工作流重试，重试之前推 decision_retry。
                        step = await decisions.run(
                            names.TOOL_LOOP_STEP, step_input, task_queue=names.MODEL_QUEUE, result_type=ToolLoopStep,
                            step="tool_loop_step", round_seq=self._ctx.round_seq, events=events, node_id=nid, about=f"{nid}/{i}",
                        )
                    else:
                        step = await workflow.execute_activity(
                            names.TOOL_LOOP_STEP,
                            step_input,
                            task_queue=names.MODEL_QUEUE,
                            start_to_close_timeout=timeouts.MODEL_DECISION_START_TO_CLOSE,
                            retry_policy=timeouts.DECISION_RETRY,
                            result_type=ToolLoopStep,
                        )
            except decisions.DecisionExhausted as exhausted:
                return _failed(DEFAULT_ROLE, f"tool_loop 第 {i} 步失败（试了 {exhausted.attempts} 次）：{failure_message(exhausted.last)}", failure_class(exhausted.last))
            except FailureError as err:
                if caused_by_cancel(err):
                    raise asyncio.CancelledError() from err
                return _failed(DEFAULT_ROLE, f"tool_loop 第 {i} 步失败：{failure_message(err)}", failure_class(err))
            self.spent_usd += float(step.cost_usd or 0.0)
            vendor = step.vendor or vendor
            if step.action == "finish":
                return StageResult(role=DEFAULT_ROLE, status="succeeded", result=step.result, cost_usd=round(self.spent_usd - spent_before, 6), vendor=vendor)
            entry: dict[str, Any] = {"tool": step.tool, "args": step.args}
            if step.tool not in tool_effects:
                entry["error"] = f"工具 {step.tool} 不在 {nid} 的可用列表里"
            else:
                effect = tool_effects[step.tool]
                # TMP 计划第 3 节：工具调用次数用完就不再调用（审批之前查，不问用不上的审批）。
                stop = self._budget_stop(nid, before_call=True)
                if stop is not None:
                    return stop
                call_id = sp.child_id(nid, 2 * i)
                gate: GateRecord | None = None
                if effect == "write_irreversible" and workflow.patched(TOOL_LOOP_APPROVAL_PATCH):
                    gate, rejection, reason = await self._tool_approval(call_id, step)
                    if reason:
                        return _denied(DEFAULT_ROLE, reason)
                    if gate is None:
                        history.append({**entry, **rejection})
                        rejections += 1
                        if rejections >= MAX_TOOL_REJECTIONS:
                            return _denied(DEFAULT_ROLE, f"{nid} 的不可逆工具调用被拒绝 {rejections} 次（含审批超时），交给 Reviewer")
                        continue
                target = tool_target(step.args)
                self.tool_calls += 1
                if events is not None:
                    await events.tool_status(self._ctx.round_seq, nid, i, step.tool, "running", target=target)
                called_at = workflow.now()
                try:
                    async with self._writer(effect, env), self._slot():
                        entry["observation"] = await workflow.execute_activity(
                            names.CALL_TOOL,
                            ToolCallInput(
                                tenant=self._ctx.tenant,
                                task_id=self._ctx.task_id,
                                round_seq=self._ctx.round_seq,
                                node_id=call_id,
                                tool=step.tool,
                                args=step.args,
                                effect=effect,
                                gate=gate,
                            ),
                            task_queue=names.PROGRAM_QUEUE,
                            start_to_close_timeout=timeouts.DEFAULT_NODE_TIMEOUT,
                            retry_policy=RetryPolicy(maximum_attempts=1 if effect in sp.WRITE_EFFECTS else 3),
                            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                        )
                except FailureError as err:
                    if caused_by_cancel(err):
                        raise asyncio.CancelledError() from err
                    entry["error"] = failure_message(err)
                if "error" not in entry and _lists_empty_root(step.tool, step.args, entry.get("observation")):
                    # TMP 计划第 2 节：工作区根是空的，交给 Reviewer 的硬信号。
                    marks.add("environment")
                if events is not None:
                    took = int((workflow.now() - called_at).total_seconds() * 1000)
                    if "error" in entry:
                        await events.tool_status(self._ctx.round_seq, nid, i, step.tool, "failed", target=target, duration_ms=took, error=str(entry["error"]))
                    else:
                        await events.tool_status(
                            self._ctx.round_seq, nid, i, step.tool, "succeeded", target=target, duration_ms=took,
                            startup_ms=sandbox_startup_ms(entry.get("observation")),
                        )
            history.append(entry)
            if sp.repeated_error(history):
                return _failed(DEFAULT_ROLE, f"repeated_error：同一错误连续出现 {sp.REPEATED_ERROR_LIMIT} 次：{entry['error']}")
        return _failed(DEFAULT_ROLE, f"max_iterations：{limit} 步内没有完成")

    async def _tool_approval(self, call_id: str, step: ToolLoopStep) -> tuple[GateRecord | None, dict[str, Any], str]:
        """tool_loop 里一次不可逆工具调用前的人工审批，审批 id 是 <调用 id>.1；超时按拒绝处理。

        返回 (审批记录, 拒绝时送回循环的观察, 不能审批的原因)：通过时只有审批记录；拒绝或超时时只有观察；
        没有人工审批通道或发不出问题时只有原因，整个 Goal Node 记为 denied。
        """
        if self._human is None:
            return None, {}, f"这里没有人工审批通道，不能调用不可逆工具 {step.tool}"
        gate_id = sp.child_id(call_id, 1)
        args = json.dumps(step.args, ensure_ascii=False, sort_keys=True)
        text = f"批准调用 {step.tool} 吗？参数：{args}。这是不可撤销的操作。"
        reply, error = await self._ask_and_wait(gate_id, text, sp.APPROVAL_OPTIONS, self.policy.approval_timeout_s, kind="tool_approval")
        if error:
            return None, {}, error
        if reply is None:
            return None, {"rejected": True, "timed_out": True}, ""
        if reply.option != "approve":
            return None, {"rejected": True, "guidance": reply.guidance, "replied_by": reply.replied_by_user_id}, ""
        return GateRecord(gate_node=gate_id, option=reply.option, approved_by=reply.replied_by_user_id), {}, ""

    async def _sub_agent(self, node: dict[str, Any], role: str) -> StageResult:
        ceiling = str(node.get("effect_ceiling") or "read")
        if sp.effect_rank(ceiling) > sp.effect_rank("read"):
            return _denied(role, f"sub_agent 只读，effect_ceiling 不能是 {ceiling}")
        effects, reason = await self._check_tools(node, ceiling, read_only=True)
        if effects is None:
            return _denied(role, reason)
        try:
            async with self._slot():
                out = await workflow.execute_child_workflow(
                    names.SUB_AGENT_WORKFLOW,
                    SubAgentInput(
                        tenant=self._ctx.tenant,
                        task_id=self._ctx.task_id,
                        round_seq=self._ctx.round_seq,
                        node=node,
                        effect_ceiling=ceiling,
                        tools=effects,
                        policy=self.policy,
                        budget_usd=self._ctx.budget_usd,
                        spent_before_usd=self._budget()["spent_usd"],
                        **self._child_budgets(),
                    ),
                    id=sp.subworkflow_id(self._ctx.task_id, self._ctx.round_seq, node["id"]),
                    task_queue=names.TASK_QUEUE,
                    memo=_memo(self._ctx.tenant),
                    result_type=StageResult,
                )
        except FailureError as err:
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            return _failed(role, f"sub_agent 失败：{failure_message(err)}", failure_class(err))
        # 子 agent 的花费与工具调用次数带回父 Stage（成本口径的人定方案；TMP 计划第 3 节）。
        self.spent_usd += float(out.cost_usd or 0.0)
        self.tool_calls += int(out.tool_calls or 0)
        return _as(out, role)

    def _child_budgets(self) -> dict[str, Any]:
        """TMP 计划第 3 节：交给子工作流的时间与工具调用预算（子工作流里没有人工等待，自己按启动以来的时间累加）。"""
        ctx = self._ctx
        return {
            "tool_calls_limit": ctx.tool_calls_limit,
            "tool_calls_before": ctx.tool_calls_before + self.tool_calls,
            "wall_clock_limit_s": ctx.wall_clock_limit_s,
            "active_before_s": ctx.active_s() if ctx.active_s is not None else 0.0,
        }

    async def _sub_task(self, node: dict[str, Any], env: _Env, role: str) -> StageResult:
        """嵌套运行一个完整的 Task Loop，深度不超过 2（附录 E.1）；超过时拒绝并交给 Reviewer。"""
        parent = self._ctx.task_input
        if parent is None:
            return _denied(role, "sub_task 只能在 Task 工作流里展开")
        if self._ctx.depth + 1 > sp.MAX_SUB_TASK_DEPTH:
            return _denied(role, f"sub_task 嵌套深度超过上限 {sp.MAX_SUB_TASK_DEPTH}")
        now = workflow.now()
        sub_id = sp.ulid_task_id(int(now.timestamp() * 1000), workflow.random().getrandbits(80))
        inputs = struct_pb2.Struct()
        json_format.ParseDict({**node.get("inputs", {}), "done_when": node["done_when"]}, inputs)
        child = task_pb2.TaskWorkflowInput()
        child.CopyFrom(parent)
        child.task_id = sub_id
        child.message = node["goal"]
        child.inputs.CopyFrom(inputs)
        del child.attachments[:]
        child.parent_task_id = parent.task_id
        child.depth = self._ctx.depth + 1
        child.submitted_at.FromDatetime(now)
        ceiling = sp.node_effect(node, self._ctx.effect_ceiling)
        child.effect_ceiling = enums_pb2.Effect.Value("EFFECT_" + ceiling.upper())
        usd = (node.get("budget") or {}).get("usd")
        if usd is not None:
            child.budget.usd_micros = int(float(usd) * 1_000_000)
        try:
            async with self._writer(ceiling, env):
                result = await workflow.execute_child_workflow(
                    names.TASK_WORKFLOW,
                    child,
                    id=sub_id,
                    task_queue=names.TASK_QUEUE,
                    memo=_memo(self._ctx.tenant),
                    result_type=task_pb2.TaskWorkflowResult,
                )
        except FailureError as err:
            if caused_by_cancel(err):
                raise asyncio.CancelledError() from err
            return _failed(role, f"子 Task {sub_id} 失败：{failure_message(err)}", failure_class(err))
        if result.status != enums_pb2.TASK_STATUS_DONE:
            status = enums_pb2.TaskStatus.Name(result.status).removeprefix("TASK_STATUS_").lower()
            return _failed(role, f"子 Task {sub_id} 的结局是 {status}")
        value = json_format.MessageToDict(result.deliverable.value) if result.HasField("deliverable") else None
        return StageResult(role=role, status="succeeded", result=value)


def _collect(role: str, results: list[StageResult]) -> StageResult:
    """逐条目展开的汇总：按条目顺序给出 {"items": [...]}；有条目没成功时整个 Goal 失败。"""
    for i, r in enumerate(results, start=1):
        if r.status != "succeeded":
            return _failed(role, f"第 {i} 个条目没有成功：{r.error}", r.failure_class)
    return StageResult(role=role, status="succeeded", result={"items": [r.result for r in results]})


@dataclass
class SubAgentInput:
    tenant: Tenant
    task_id: str
    round_seq: int
    # sub_agent 模式的 Goal Node，inputs 已解析
    node: dict[str, Any]
    effect_ceiling: str
    # 可用的工具 → effect，展开时已校验不高于 effect_ceiling
    tools: dict[str, str]
    policy: StagePolicy
    # Task 的预算与启动时已花的钱：子 agent 的 tool_loop 每一步之前按它查（成本口径的人定方案）；之前的历史没有，按没有预算。
    budget_usd: float | None = None
    spent_before_usd: float = 0.0
    # TMP 计划第 3 节：工具调用次数的上限与启动时已发出的、时间预算（秒）与启动时已用的；0 不限，之前的历史没有。
    tool_calls_limit: int = 0
    tool_calls_before: int = 0
    wall_clock_limit_s: float = 0.0
    active_before_s: float = 0.0


def _child_context(inp: SubAgentInput | SubgraphInput, **kw: Any) -> RunContext:
    """子工作流的 RunContext：版本分支在子工作流自己的历史里问；时间按启动以来的工作流时间累加（子工作流里没有人工等待）。"""
    started = workflow.now()
    budgets = workflow.patched(BUDGETS_PATCH)
    return RunContext(
        tenant=inp.tenant,
        task_id=inp.task_id,
        round_seq=inp.round_seq,
        effect_ceiling=inp.effect_ceiling,
        depth=sp.MAX_SUB_TASK_DEPTH,
        policy=inp.policy,
        t09=workflow.patched(POLICY_PATCH),
        budget_usd=inp.budget_usd,
        spent_before_usd=inp.spent_before_usd,
        r01=workflow.patched(R01_FIXES_PATCH),
        budgets=budgets,
        tool_calls_limit=inp.tool_calls_limit,
        tool_calls_before=inp.tool_calls_before,
        wall_clock_limit_s=inp.wall_clock_limit_s,
        active_s=(lambda: inp.active_before_s + (workflow.now() - started).total_seconds()) if budgets else None,
        decision_retry=workflow.patched(DECISION_RETRY_PATCH),
        **kw,
    )


@workflow.defn(name=names.SUB_AGENT_WORKFLOW)
class SubAgentWorkflow:
    """sub_agent：独立上下文的子 agent，在自己的事件历史里跑 tool_loop，只交回结果（附录 E.1）。结果的 cost_usd 是它花的钱，父 Stage 加上。"""

    @workflow.run
    async def run(self, inp: SubAgentInput) -> StageResult:
        patches.at_start()
        ctx = _child_context(inp)
        runner = StageRunner(ctx, human=None)
        await runner._ensure_policy()
        result = await runner.tool_loop(inp.node, isolated=True, tool_effects=dict(inp.tools), env=_Env(results={}))
        return replace(result, cost_usd=round(runner.spent_usd, 6), tool_calls=runner.tool_calls)


@dataclass
class SubgraphInput:
    tenant: Tenant
    task_id: str
    round_seq: int
    goal_id: str
    # 这一批条目的链（Node id 已编号）与每条链给出结果的 Node id
    chains: list[list[dict[str, Any]]]
    collect: list[str]
    task_state: dict[str, Any]
    effect_ceiling: str
    policy: StagePolicy
    # Goal Node 的输入，供 $goal 引用
    goal_inputs: dict[str, Any] = field(default_factory=dict)
    # Task 的预算与启动时已花的钱，给子图里的 budget_check（成本口径的人定方案）；之前的历史没有，按没有预算。
    budget_usd: float | None = None
    spent_before_usd: float = 0.0
    # TMP 计划第 3 节：同 SubAgentInput。
    tool_calls_limit: int = 0
    tool_calls_before: int = 0
    wall_clock_limit_s: float = 0.0
    active_before_s: float = 0.0


@dataclass
class SubgraphOutput:
    # collect 的 Node id → 结果
    results: dict[str, StageResult]
    # 这一批花的钱，父 Stage 加上
    cost_usd: float = 0.0
    # 这一批发出的工具调用，父 Stage 加上（TMP 计划第 3 节）
    tool_calls: int = 0


@workflow.defn(name=names.SUBGRAPH_WORKFLOW)
class SubgraphWorkflow:
    """template 的大批量展开：一批条目的子图在子工作流里跑，控制单个工作流的事件历史（计划 S-04 难点“扇出上限”）。"""

    @workflow.run
    async def run(self, inp: SubgraphInput) -> SubgraphOutput:
        patches.at_start()
        ctx = _child_context(inp, task_state=inp.task_state)
        runner = StageRunner(ctx, human=None)
        await runner._ensure_policy()
        nodes = [n for chain in inp.chains for n in chain]
        await runner._ensure_catalog_for(nodes)
        done = await runner._run_dag(nodes, _Env(results={}, goal_inputs=inp.goal_inputs))
        return SubgraphOutput(results={c: done[c] for c in inp.collect}, cost_usd=round(runner.spent_usd, 6), tool_calls=runner.tool_calls)
