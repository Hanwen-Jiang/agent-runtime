"""故障注入用的 Worker（计划 S-07）。

工作流、队列划分与 Node Executor 都与 `python -m agentruntime_worker` 相同，只是 Node Executor 的依赖换成
能按故障计划（e2e_support.faults）出错、并记下调用的版本：

- ledger 工具：program 节点经 ToolDriver 的开发实现调用，写到 Worker 进程之外的外部 ledger（e2e_support.ledger）。
  四个工具代表外部 API 的三种能力，见 LEDGER_TOOLS。
- 模型供应商替身：同一档位两家供应商；供应商超时时先重试，超过次数就切换到同档位的另一家（设计书 3.3.1 第 3 条）。
  设了 AR_MODEL_GATEWAY_ADDRESS 时改用真的 Go Model Gateway（场景 1、4 另有对它跑的用例，T-13），供应商一侧的故障由假的上游端点注入。
- ObjectStore、HumanChannel 外面包一层：按故障计划出错，并把调用记进调用记录。
- 桩 Planner：Task inputs 里有 fault_stage 时直接把它当作 Stage（单个 Round）；桩计划的 model 节点按固定版本选模板版本。
- 桩 Reviewer：有结果未知（unknown）的 Node 时判 abort，其余同 S-03 的桩；判定前把看到的各 Node 状态记进调用记录。

环境变量：AR_FAULT_PLAN（故障计划文件）、AR_FAULT_LEDGER_DIR（外部 ledger 目录），其余同 Worker。

    python -m e2e_support.fault_worker --queues task,decision,model,program
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from google.protobuf import json_format, struct_pb2
from temporalio import activity
from temporalio.client import Client

from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.dev.cache import InMemoryCacheProvider
from agentruntime_core.dev.human_channel import CliHumanChannelProvider
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.sandbox import LocalSubprocessSandboxProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.enums import Effect
from agentruntime_core.errors import TransientError
from agentruntime_core.providers.human_channel import Question, QuestionStatus
from agentruntime_core.providers.tool_driver import ToolInvocation, ToolSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.calllog import CallLog
from agentruntime_worker.activities.stubs import StubActivities, StubModelGateway
from agentruntime_worker.activities.types import PlanRoundInput, PlanRoundOutput, ReviewRoundInput
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.dev import TemporalReplySink, data_root, dev_object_store, dev_secret_store
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway
from agentruntime_worker.executor.node_executor import ExecutorDeps, NodeExecutor, workflow_question_id
from agentruntime_worker.executor.schemas import DictSchemaResolver
from agentruntime_worker.worker import QUEUE_COMPONENTS, build_workers, connect, parse_queues

from e2e_support.faults import CRASH_EXIT_CODE, ENV_FAULT_LEDGER_DIR, ENV_FAULT_PLAN, FaultPlan, Rule
from e2e_support.ledger import ExternalLedger

# ledger 工具的超时：lose_response 让响应晚于它回来，调用方只看到超时。
TOOL_TIMEOUT_S = 5
# 名字 → (effect, 外部按 Idempotency-Key 去重, 支持按幂等键查询)
LEDGER_TOOLS: dict[str, tuple[str, bool, bool]] = {
    "ledger_post": ("write_reversible", True, False),
    "ledger_append": ("write_reversible", False, True),
    "ledger_transfer": ("write_irreversible", False, True),
    "ledger_wire": ("write_irreversible", False, False),
}
LEDGER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ref"],
    "properties": {"ref": {"type": "string"}, "amount": {"type": "integer"}, "echo_kb": {"type": "integer", "minimum": 0}},
    "additionalProperties": False,
}
LEDGER_OUTPUT_SCHEMA: dict[str, Any] = {"type": "object", "required": ["txn_id"], "properties": {"txn_id": {"type": "string"}}}

# 模型供应商替身：同一档位的两家；每家的单次超时与切换前的尝试次数。
TIER = "stub@low"
VENDORS = ("stub-a", "stub-b")
VENDOR_TIMEOUT_S = 1.5
VENDOR_ATTEMPTS = 2

# 调用记录里的名字（activity 名之外的）。
VENDOR_CALL = "vendor_call"
OBJECT_STORE = "object_store"
HUMAN_CHANNEL = "human_channel"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def act(rule: Rule) -> None:
    """执行一条规则的故障。timeout 由调用方自己处理（它要接着重试或切换）。"""
    if rule.action == "crash":
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(CRASH_EXIT_CODE)
    if rule.action == "hang":
        await asyncio.Event().wait()
    if rule.action == "transient":
        raise TransientError("unavailable", f"注入的故障（{rule.id}）：依赖暂时不可用")
    if rule.action == "lose_response":
        await asyncio.sleep(TOOL_TIMEOUT_S + 2)
    if rule.action == "timeout":
        await asyncio.sleep(VENDOR_TIMEOUT_S)


# --- ledger 工具 ---


def register_ledger_tools(provider: LocalToolDriverProvider, ledger: ExternalLedger, plan: FaultPlan) -> None:
    """登记 ledger 工具。支持查询的工具把 lookup 交给外部 ledger：Worker 重启后，ToolDriver 进程内的幂等记录没了，
    外部的记录还在。"""
    for name, (effect, dedup, lookup) in LEDGER_TOOLS.items():
        spec = ToolSpec(
            name=name,
            version="v1",
            description=f"外部 ledger 的写操作（{'按 Idempotency-Key 去重' if dedup else '不去重'}，{'支持' if lookup else '不支持'}按幂等键查询）",
            input_schema=LEDGER_INPUT_SCHEMA,
            output_schema=LEDGER_OUTPUT_SCHEMA,
            effect=Effect(effect),
            permissions=frozenset(),
            timeout=timedelta(seconds=TOOL_TIMEOUT_S),
            supports_idempotency_lookup=lookup,
        )
        provider.register(spec, _ledger_handler(name, dedup, ledger, plan), lookup=_ledger_lookup(name, ledger, plan) if lookup else None)


def _ledger_handler(name: str, dedup: bool, ledger: ExternalLedger, plan: FaultPlan) -> Any:
    async def handler(inv: ToolInvocation) -> Any:
        arguments = dict(inv.arguments)
        context = f"{name} {_canonical(arguments)}"
        rule = plan.fire("tool.before_apply", context)
        if rule is not None:
            await act(rule)
        result = ledger.apply(name, inv.idempotency_key, arguments, dedup=dedup)
        rule = plan.fire("tool.after_apply", context)
        if rule is not None:
            await act(rule)
        return result

    return handler


def _ledger_lookup(name: str, ledger: ExternalLedger, plan: FaultPlan) -> Any:
    async def lookup(query: Any) -> Any | None:
        key = str(query.idempotency_key)
        rule = plan.fire("tool.lookup", f"{name} {key}")
        if rule is not None:
            await act(rule)
        entry = ledger.find(name, key)
        return entry["result"] if entry is not None else None

    return lookup


# --- 模型供应商替身 ---


class FailoverStubGateway:
    """Model Gateway 的替身：同一档位两家供应商，供应商超时先重试、再切换；都不行时报 transient。

    真正干活的是 StubModelGateway（S-03 的桩，按 mode 行事）。inputs.result_kb 让结果变大（场景 10）。
    """

    def __init__(self, inner: StubModelGateway, plan: FaultPlan, calls: CallLog) -> None:
        self.inner = inner
        self.plan = plan
        self.calls = calls

    async def generate(self, request: mg.GenerateRequest) -> mg.GenerateResponse:
        task_id, node_id = request.call.task_id, request.call.node_id
        user = next((m.text for m in request.generation.messages if m.role == mg.CHAT_ROLE_USER), "{}")
        try:
            payload = json.loads(user)
        except json.JSONDecodeError:
            payload = {}
        inputs = payload.get("inputs") or {}
        template = str(payload.get("template") or "")
        context = _canonical(inputs)
        for vendor in VENDORS:
            for attempt in range(1, VENDOR_ATTEMPTS + 1):
                self.calls.record(VENDOR_CALL, task_id, "start", vendor=vendor, node_id=node_id, template=template, vendor_attempt=attempt)
                rule = self.plan.fire("model.vendor", f"{vendor} {context}")
                if rule is not None:
                    await act(rule)
                    self.calls.record(VENDOR_CALL, task_id, "timeout", vendor=vendor, node_id=node_id)
                    await asyncio.sleep(0.1 * attempt)  # 退避
                    continue
                response = await self.inner.generate(request)
                response.model.CopyFrom(mg.ModelChoice(model_profile_id=f"{vendor}/model", tier=TIER, vendor=vendor))
                if inputs.get("result_kb"):
                    big = struct_pb2.Value()
                    json_format.ParseDict({"text": "x" * (int(inputs["result_kb"]) * 1024), "final": True}, big)
                    response.result.CopyFrom(big)
                self.calls.record(VENDOR_CALL, task_id, "end", vendor=vendor, node_id=node_id, template=template, tier=TIER)
                return response
        raise TransientError("vendor_unavailable", f"{TIER} 档位的供应商都超时")

    async def embed(self, request: mg.EmbedRequest) -> mg.EmbedResponse:
        return await self.inner.embed(request)


# --- ObjectStore、HumanChannel ---


def _task_of_key(key: str) -> str:
    parts = key.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "node-results" else ""


class _FaultyObjectStore:
    def __init__(self, inner: Any, plan: FaultPlan, calls: CallLog) -> None:
        self._inner = inner
        self._plan = plan
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def put(self, key: str, data: bytes, **kwargs: Any) -> Any:
        rule = self._plan.fire("objects.put", key)
        if rule is not None:
            self._calls.record(OBJECT_STORE, _task_of_key(key), "put_failed", key=key)
            await act(rule)
        info = await self._inner.put(key, data, **kwargs)
        self._calls.record(OBJECT_STORE, _task_of_key(key), "put", key=key, size=len(data))
        return info


class FaultyObjectStoreProvider:
    def __init__(self, inner: LocalDiskObjectStoreProvider, plan: FaultPlan, calls: CallLog) -> None:
        self._inner = inner
        self._plan = plan
        self._calls = calls

    def bind(self, tenant: TenantContext) -> Any:
        return _FaultyObjectStore(self._inner.bind(tenant), self._plan, self._calls)

    def bind_platform(self, purpose: str) -> Any:
        return _FaultyObjectStore(self._inner.bind_platform(purpose), self._plan, self._calls)

    async def aclose(self) -> None:
        await self._inner.aclose()


class _RecordingHumanChannel:
    def __init__(self, inner: Any, calls: CallLog) -> None:
        self._inner = inner
        self._calls = calls

    async def ask(self, question: Question) -> None:
        await self._inner.ask(question)
        self._calls.record(HUMAN_CHANNEL, question.task_id, "ask", question_id=workflow_question_id(question.question_id))

    async def cancel(self, question_id: str) -> None:
        # 记下撤回请求本身：开发用的命令行通道在 Worker 进程里，Worker 被杀后它不再记得之前的问题。
        self._calls.record(HUMAN_CHANNEL, question_id.split("/")[0], "cancel", question_id=workflow_question_id(question_id))
        await self._inner.cancel(question_id)

    async def status(self, question_id: str) -> QuestionStatus:
        return await self._inner.status(question_id)


class RecordingHumanChannelProvider:
    def __init__(self, inner: CliHumanChannelProvider, calls: CallLog) -> None:
        self._inner = inner
        self._calls = calls

    def bind(self, tenant: TenantContext) -> Any:
        return _RecordingHumanChannel(self._inner.bind(tenant), self._calls)

    def bind_platform(self, purpose: str) -> Any:
        return _RecordingHumanChannel(self._inner.bind_platform(purpose), self._calls)

    async def aclose(self) -> None:
        await self._inner.aclose()


# --- 桩 Planner 与 Reviewer ---


def _pin_template(node: dict[str, Any], pinned: Mapping[str, Any]) -> dict[str, Any]:
    """模板版本取 Task 固定的版本（Planner 按固定版本选模板，设计书 3.6“发布新版本”）。"""
    template = str(node.get("template") or "")
    name, _, _ = template.partition("@")
    version = (pinned.get("node_templates") or {}).get(name)
    return {**node, "template": f"{name}@{version}"} if version else node


class FaultStubActivities(StubActivities):
    @activity.defn(name=names.PLAN_ROUND)
    async def plan_round(self, inp: PlanRoundInput) -> PlanRoundOutput:
        nodes = inp.inputs.get("fault_stage")
        if nodes is None:
            out = await super().plan_round(inp)
            out.stage["nodes"] = [_pin_template(n, inp.pinned) for n in out.stage["nodes"]]
        else:
            self.calls.record(names.PLAN_ROUND, inp.task_id, "start", round_seq=inp.round_seq)
            stage = {"seq": inp.round_seq, "goal": "故障注入用例", "exit_when": "deliver 节点给出结果", "nodes": list(nodes)}
            self.calls.record(names.PLAN_ROUND, inp.task_id, "end", round_seq=inp.round_seq)
            out = PlanRoundOutput(stage=stage, triage_level="fast")
        self.calls.record(names.PLAN_ROUND, inp.task_id, "pinned", round_seq=inp.round_seq, pinned=inp.pinned)
        return out

    @activity.defn(name=names.REVIEW_ROUND)
    async def review_round(self, inp: ReviewRoundInput) -> dict[str, Any]:
        results = dict(sorted(inp.stage_results.items()))
        statuses = {nid: r.status for nid, r in results.items()}
        errors = {nid: {"failure_class": r.failure_class, "error": r.error} for nid, r in results.items() if r.status != "succeeded"}
        self.calls.record(names.REVIEW_ROUND, inp.task_id, "saw", round_seq=inp.round_seq, statuses=statuses, errors=errors)
        unknown = [nid for nid, status in statuses.items() if status == "unknown"]
        if not unknown:
            return await super().review_round(inp)
        self.calls.record(names.REVIEW_ROUND, inp.task_id, "start", round_seq=inp.round_seq)
        verdict = {
            "verdict": "abort",
            "reason": f"写操作结果未知（{', '.join(unknown)}），需要人工确认",
            "abort": {"completed": [], "not_completed": unknown},
        }
        self.calls.record(names.REVIEW_ROUND, inp.task_id, "end", round_seq=inp.round_seq, verdict="abort")
        return verdict


# --- 组装与入口 ---


def faulty_objects(config: WorkerConfig, plan: FaultPlan) -> FaultyObjectStoreProvider:
    """会按故障计划出错的 ObjectStore：Node Executor 的大结果与 Claim-Check（S-06）都经过它。"""
    return FaultyObjectStoreProvider(dev_object_store(config, data_root(config)), plan, CallLog(config.call_log))


def build_fault_components(
    config: WorkerConfig, client: Client, plan: FaultPlan, ledger: ExternalLedger, objects: FaultyObjectStoreProvider
) -> tuple[FaultStubActivities, NodeExecutor]:
    root = data_root(config)
    calls = CallLog(config.call_log)
    tools = LocalToolDriverProvider()
    register_ledger_tools(tools, ledger, plan)
    deps = ExecutorDeps(
        tools=tools,
        objects=objects,
        secrets=dev_secret_store(config, root),
        cache=InMemoryCacheProvider(),
        humans=RecordingHumanChannelProvider(CliHumanChannelProvider(TemporalReplySink(client), output=sys.stderr), calls),
        telemetry=ConsoleTelemetryProvider(stream=sys.stderr),
        # 设了 AR_MODEL_GATEWAY_ADDRESS 时经真的 Go Model Gateway（场景 1、4 的 T-13 用例；故障由假的上游注入），否则用替身。
        models=GrpcModelGateway(config.model_gateway_address) if config.model_gateway_address else FailoverStubGateway(StubModelGateway(config), plan, calls),
        schemas=DictSchemaResolver({}),
        sandbox=LocalSubprocessSandboxProvider(root / "sandbox"),
    )
    return FaultStubActivities(config), NodeExecutor(deps)


async def serve(config: WorkerConfig, queues: Sequence[str], plan: FaultPlan, ledger: ExternalLedger) -> None:
    objects = faulty_objects(config, plan)
    client = await connect(config, objects=objects)
    stubs, executor = build_fault_components(config, client, plan, ledger, objects)
    async with contextlib.AsyncExitStack() as stack:
        for worker in build_workers(client, config, queues, stubs=stubs, executor=executor):
            await stack.enter_async_context(worker)
        await asyncio.Event().wait()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m e2e_support.fault_worker", description="故障注入用的 Worker（计划 S-07）")
    parser.add_argument("--queues", default=",".join(QUEUE_COMPONENTS))
    args = parser.parse_args(argv)
    try:
        queues = parse_queues(args.queues)
        config = WorkerConfig.from_env(os.environ)
        plan = FaultPlan(Path(os.environ[ENV_FAULT_PLAN]))
        ledger = ExternalLedger(Path(os.environ[ENV_FAULT_LEDGER_DIR]))
    except (ValueError, KeyError) as err:
        parser.error(str(err))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(config, queues, plan, ledger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
