"""tool_loop 的一步“思考”经 Model Gateway（附录 E.1；Context Builder 的第一个使用者）。AR_TOOL_LOOP=model 时代替桩。

上下文由 Context Builder 组装：system 是 tool_loop 的平台提示，brief 是 Goal、验收条件、输入与 Task Plan，
history 是此前的动作与观察，其后是 Task State 的精确状态，最后的 status 是进度、最近的动作与错误；工具定义随请求的 tools 给出，
同一份交给 Context Builder 计入阈值。模型发起工具调用就返回
action = tool（只取第一个调用，一步一个动作）；不再调用工具时，结构化结果（没有就用文本）作为 action = finish 的结果。
模型的 rationale 与压缩情况只写入日志（trace），不进上下文、不传给下游。
这一步的上下文占比随结果带回（context_usage），工作流推进度事件（docs/plan/running-task-interaction.md 第 3 节）。

TMP 计划第 11.15 节：回答不合格（调了没给的工具、输出不是合法 JSON）时在这个 activity 里告诉模型错在哪，再问，问满了才让这一步失败；
节点能改文件却连续只读时提醒它动手。这两段话只在这一步的上下文里，不进 history。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from google.protobuf import json_format, struct_pb2
from temporalio import activity

from agentruntime.common.v1 import enums_pb2, payload_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import InvalidError

from agentruntime_worker import names
from agentruntime_worker.personal_config import TaskPersonalConfig, preference
from agentruntime_worker.activities.types import ToolLoopStep, ToolLoopStepInput
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.memory import Searcher, node_experiences, searcher
from agentruntime_worker.context.builder import ContextBuilder, ContextConfig, ContextRequest, measured, tenant_context, task_family
from agentruntime_worker.executor.heartbeat import heartbeating
from agentruntime_worker.executor.model_gateway import ModelGateway, billed_cost_usd, with_billed_cost
from agentruntime_worker.executor.schemas import OutputSchemaResolver
from agentruntime_worker.executor.tiers import SUB_AGENT_TIER, sub_agent_tier
from agentruntime_worker.executor.tool_schema import format_schema_correction, validate_tool_arguments
from agentruntime_worker.workflows.verification import only_reads
from agentruntime_worker.workflows.events import public_progress
from agentruntime_worker.session.transcript import TranscriptSource, transcript_text
from agentruntime_worker.tools.catalog import ToolCatalog

from agentruntime_worker.prompts import EXECUTOR_IDENTITY

TOOL_LOOP_TIER = "strong@medium"
# N16-R（附录 F）：子 Agent 的 Tier 见 executor.tiers；主流程的 tool_loop 不换（提示缓存）。
# 放进 brief 的之前的对话（字）：上下文的预算要留给工具的结果（TMP 计划第 11.13 节）。
CONVERSATION_CHARS = 8000
SYSTEM_PROMPT = EXECUTOR_IDENTITY
# 回答不合格时改正几次（再问的次数）。
MAX_CORRECTIONS = 2
CORRECTION_HEADER = "【上一次的回答不能用】"
# 节点能改文件、又连续这么多次只读时提醒。基线里没有补丁的 3 题都读满了额度，一行没改。
READ_ONLY_REMINDER_AT = 20
READ_ONLY_REMINDER = "【提醒】你已经连续读了很多内容，还没有改过任何东西。如果已经看懂了问题，现在就动手改；还缺什么就只查那一处，不要继续翻。"
_WRITE_CAPABLE_TOOLS = frozenset({"repo_edit", "apply_patch", "shell_run"})


def _wire_name(name: str) -> str:
    """给模型的工具名：点换成 __（mcp.github.search → mcp__github__search）；平台内工具没有点，不变。"""
    return name.replace(".", "__")


def _value(data: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return value


def _needs_read_only_reminder(node: dict[str, Any], history: list[dict[str, Any]]) -> bool:
    if not _WRITE_CAPABLE_TOOLS & {str(t) for t in node.get("tools") or []}:
        return False
    streak = 0
    for step in reversed(history):
        if "tool" not in step:
            continue  # 用户的纠偏之类，不是工具调用
        if not only_reads(step.get("tool"), step.get("args")):
            return False
        streak += 1
        if streak >= READ_ONLY_REMINDER_AT:
            return True
    return False


def _correction(err: InvalidError, tools: list[mg.ToolSpec]) -> str:
    if err.code == "unexpected_tool_call":
        names_ = "、".join(t.name for t in tools)
        allowed = f"这一步只能用这些工具：{names_}。" if names_ else "这一步没有可用的工具，直接按要求的结构给出结果。"
        return f"{CORRECTION_HEADER}{err.message}。{allowed}"
    if err.code == "bad_tool_arguments":
        return f"{CORRECTION_HEADER}工具的参数不是合法的 JSON。重新调用，把参数写完整。"
    return f"{CORRECTION_HEADER}结果不是合法的 JSON。要给出结果就只输出符合要求结构的 JSON，不加别的文字；还要继续做就调用工具。"


_CORRECTABLE = frozenset({"unexpected_tool_call", "bad_structured_output", "bad_tool_arguments"})


class ModelToolLoop:
    def __init__(
        self, gateway: ModelGateway, builder: ContextBuilder, *, tier: str = TOOL_LOOP_TIER, tools: ToolCatalog | None = None,
        transcript: TranscriptSource | None = None, mcp_tools: Callable[[], frozenset[str]] | None = None, memory: Searcher | None = None,
        sub_agent_tier: str | None = None, personal: TaskPersonalConfig | None = None, attachments: Any = None,
        runtime_capabilities: Any | None = None, schema_validation: bool = True,
    ) -> None:
        self.attachments = attachments
        self.runtime_capabilities = runtime_capabilities
        self.schema_validation = schema_validation
        self.memory = memory
        self.personal = personal
        # 子 Agent（isolated 的步）用的 Tier；None 时与主流程同一个。
        self.sub_agent_tier = sub_agent_tier
        # 给了 ToolCatalog 时，工具的说明与参数 schema 取自登记，没登记的不给模型（T-10）；没给时只有名字。
        self.tools = tools
        # 现在登记了的 MCP 工具（Worker 启动时才连上 MCP Server，所以是函数）：上下文占比里单独一类。
        self.mcp_tools = mcp_tools
        self.gateway = gateway
        self.builder = builder
        self.tier = tier
        self.transcript = transcript

    def _tier(self, inp: ToolLoopStepInput) -> str:
        """主流程用 self.tier；子 Agent 用 sub_agent_tier，Node 写了更小的 tier 时用它（N16-R）。开关关着时都是 self.tier。"""
        if not (inp.isolated and self.sub_agent_tier):
            return self.tier
        return sub_agent_tier(self.sub_agent_tier, inp.node.get("tier"))

    async def step(self, inp: ToolLoopStepInput) -> ToolLoopStep:
        node = inp.node
        offered = [str(t) for t in node.get("tools") or []]
        file_constrained = False
        if inp.isolated:
            from agentruntime_worker.files.scopes import ALLOWED_CHILD_TOOLS, parent_has_file_inputs
            delegated = (node.get("inputs") or {}).get("file_scopes")
            parent_files = await parent_has_file_inputs(self.builder.objects, inp.tenant, inp.task_id)
            file_constrained = delegated is not None or bool(parent_files)
            if file_constrained:
                offered = [tool for tool in offered if tool in ALLOWED_CHILD_TOOLS] if delegated else []
        tools = await self._tool_specs(inp, offered)
        leased = await self.tools.mcp_names(inp.tenant, inp.task_id) if self.tools is not None else frozenset()
        turns = await self.transcript.load(inp.tenant, inp.task_id) if self.transcript is not None and not inp.isolated else ()
        personal = await self.personal.load(inp.tenant.tenant_id, inp.task_id) if self.personal is not None else None
        from agentruntime_worker.executor.image_input import ImageInputs, prepare_images
        images = ImageInputs()
        conversation = transcript_text(turns, max_chars=CONVERSATION_CHARS)
        if self.attachments is not None and not inp.isolated:
            from agentruntime_worker.tools.attachments import manifest_text
            files = await self.attachments.describe(inp.tenant.tenant_id, inp.task_id, inp.tenant.workspace_id)
            if files:
                images = await prepare_images(self.gateway, inp.tenant, files)
                conversation += "\n\n" + manifest_text(files, native_images=bool(images.messages))
        collection_loader = getattr(self.attachments, "collections", None)
        if collection_loader and not inp.isolated:
            from agentruntime_worker.tools.attachments import collection_text
            conversation += "\n\n" + collection_text(await collection_loader(inp.tenant.tenant_id, inp.task_id, inp.tenant.workspace_id))
        runtime_context = ""
        if self.runtime_capabilities is not None:
            from agentruntime_worker.runtime_capabilities import context_text
            ceiling = str(node.get("effect_ceiling") or (inp.task_state.get("constraints") or {}).get("effect_ceiling") or "read")
            runtime_context = context_text(await self.runtime_capabilities.load(inp.tenant, inp.task_id, ceiling=ceiling, tool_names=[str(t) for t in node.get("tools") or []]))
        built = await self.builder.build(
            ContextRequest(
                tenant=inp.tenant,
                task_id=inp.task_id,
                round_seq=inp.round_seq,
                node=node,
                history=inp.history,
                task_state=inp.task_state,
                task_plan=inp.task_plan,
                context_version=inp.context_version,
                task_family=inp.task_family,
                system=[SYSTEM_PROMPT, *([runtime_context] if runtime_context else [])],
                profile=[preference(personal.instructions)] if personal and personal.instructions else [],
                tools=tools,
                mcp_tools=(self.mcp_tools() if self.mcp_tools is not None else frozenset()) | leased,
                conversation=conversation,
                image_messages=images.messages,
                image_token_reserve=images.token_reserve,
                experiences=() if file_constrained else await node_experiences(self.memory, inp.tenant, inp.task_id, node, iteration=inp.iteration),
            )
        )
        # N-15：远程 MCP 工具的内核名带点（mcp.<连接>.<工具>），模型厂商的工具名不收点：给模型时（工具定义与此前的调用）换成 __，
        # 回来再换回。
        wire = {_wire_name(t.name): t.name for t in tools}
        tools = [mg.ToolSpec(name=_wire_name(t.name), description=t.description, params_schema=t.params_schema) for t in tools]
        for message in built.messages:
            for call in message.tool_calls:
                call.name = _wire_name(call.name)
        params = mg.GenerateParams(messages=built.messages, tools=tools)
        if _needs_read_only_reminder(node, inp.history):
            params.messages.append(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=READ_ONLY_REMINDER))
            with contextlib.suppress(Exception):
                from opentelemetry import trace

                span = trace.get_current_span()
                if span.is_recording():
                    span.set_attributes({
                        "runtime.read_only_reminder": True,
                        "runtime.signals": "excessive_reads",
                        "runtime.task_id": inp.task_id,
                        "runtime.task_family": inp.task_family,
                        "skill.name": str(node.get("skill") or (node.get("metadata") or {}).get("skill") or "explore-repo"),
                        "failure.reason": "excessive_reads",
                    })
        if isinstance(node.get("output_schema"), dict):
            params.output_schema.CopyFrom(payload_pb2.JsonPayload(value=_value(node["output_schema"])))
        response, billed = await self._generate(inp, params, tools)
        if activity.in_activity():
            activity.logger.info(
                "tool_loop %s 第 %d 步：压缩 %s，Probe %s；思考摘要（只进 trace）：%s",
                node.get("id"),
                inp.iteration,
                built.applied or "无",
                [(p.fact.answer, p.passed) for p in built.probes] or "无",
                response.rationale[:500],
            )
        # 这一步的花费（含 Context Builder 的摘要与 Probe）计入 Round 的成本；vendor 给 Reviewer 避开生成者用。
        cost = round(response.usage.cost_usd_micros / 1_000_000 + built.cost_usd + billed, 6)
        vendor = response.model.vendor
        usage = built.usage.payload(response.model.context_window, self.builder.config.effective_tokens) if built.usage is not None else None
        if usage is not None:
            # 总数用这次实际计的输入 token（N-17），各类按估算的比例分摊。
            usage = measured(usage, int(response.usage.input_tokens))
        if response.tool_calls:
            calls = [{"tool": wire.get(call.name, call.name), "args": json_format.MessageToDict(call.arguments)} for call in response.tool_calls]
            return ToolLoopStep(action="tool", tool=calls[0]["tool"], args=calls[0]["args"], calls=calls, cost_usd=cost, vendor=vendor, context_usage=usage,
                                progress_message=public_progress(response.text))
        result = json_format.MessageToDict(response.result) if response.HasField("result") else response.text
        return ToolLoopStep(action="finish", result=result, cost_usd=cost, vendor=vendor, context_usage=usage)

    async def _generate(self, inp: ToolLoopStepInput, params: mg.GenerateParams, tools: list[mg.ToolSpec]) -> tuple[mg.GenerateResponse, float]:
        """调模型；回答不合格或工具参数未过 Schema 校验时带上改正说明再问。返回回答与之前失败调用已计费的花费（美元）。"""
        billed = 0.0
        schemas = {
            t.name: json_format.MessageToDict(t.params_schema.value)
            for t in tools
            if t.HasField("params_schema") and t.params_schema.HasField("value")
        }
        repairs = 0
        for attempt in range(MAX_CORRECTIONS + 1):
            try:
                response = await self.gateway.generate(
                    mg.GenerateRequest(
                        tenant=tenant_context(inp.tenant),
                        call=mg.CallContext(task_id=inp.task_id, node_id=inp.step_id or str(inp.node.get("id", "")), role=enums_pb2.ROLE_ACT,
                                            tier=self._tier(inp),
                                            task_family=task_family(inp.task_plan, inp.task_family)),
                        generation=params,
                    )
                )
            except InvalidError as err:
                if err.code not in _CORRECTABLE:
                    raise
                billed += billed_cost_usd(err)
                if attempt == MAX_CORRECTIONS:
                    raise with_billed_cost(err, round(billed * 1_000_000)) from None
                params.messages.append(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=_correction(err, tools)))
            else:
                if self.schema_validation and response.tool_calls:
                    schema_issue: tuple[str, list[str]] | None = None
                    for call in response.tool_calls:
                        args = json_format.MessageToDict(call.arguments) if call.HasField("arguments") else {}
                        errs = validate_tool_arguments(call.name, args, schemas.get(call.name))
                        if errs:
                            schema_issue = (call.name, errs)
                            break
                    if schema_issue is not None:
                        call_name, errs = schema_issue
                        billed += response.usage.cost_usd_micros / 1_000_000
                        repairs += 1
                        with contextlib.suppress(Exception):
                            from opentelemetry import trace

                            span = trace.get_current_span()
                            if span.is_recording():
                                span.set_attribute("tool.schema_repairs", repairs)
                        if attempt == MAX_CORRECTIONS:
                            raise with_billed_cost(InvalidError("bad_tool_arguments", "；".join(errs)), round(billed * 1_000_000))
                        params.messages.append(mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=format_schema_correction(call_name, errs)))
                        continue
                return response, billed
        raise AssertionError("循环总会返回或抛出")

    async def _tool_specs(self, inp: ToolLoopStepInput, tools: list[str]) -> list[mg.ToolSpec]:
        if self.tools is None:
            return [mg.ToolSpec(name=t, params_schema=payload_pb2.JsonPayload(value=_value({"type": "object"}))) for t in tools]
        specs = await self.tools.specs(inp.tenant, tools, inp.task_id)
        return [
            mg.ToolSpec(name=t, description=specs[t].description, params_schema=payload_pb2.JsonPayload(value=_value(dict(specs[t].input_schema))))
            for t in tools
            if t in specs
        ]


class ToolLoopActivities:
    def __init__(self, loop: ModelToolLoop) -> None:
        self.loop = loop

    @activity.defn(name=names.TOOL_LOOP_STEP)
    async def tool_loop_step(self, inp: ToolLoopStepInput) -> ToolLoopStep:
        async with heartbeating():
            return await self.loop.step(inp)

    def activities(self) -> dict[str, Any]:
        return {names.TOOL_LOOP_STEP: self.tool_loop_step}


def dev_tool_loop(config: WorkerConfig) -> ToolLoopActivities:
    """AR_TOOL_LOOP=model 时的开发组装：真正的 tool_loop 与 Context Builder，加开发实现的 ObjectStore。

    工具定义取自开发用 ToolDriver 的登记（dev_tools，与 Node Executor 的 call_tool 同一个）。Context Builder 外置的内容
    写进 context_objects(config)，模型用 context_read 从同一处取回。
    模型经 model_gateway(config)：设了 AR_MODEL_GATEWAY_ADDRESS 时走 Model Gateway；没设时是桩模型，桩模型不会发起工具调用，
    循环第一步就结束，只验证组装与链路，压缩效果见 S-11 的评测。
    """
    from agentruntime_worker.executor.dev import model_gateway
    from agentruntime_worker.session.transcript import worker_transcript
    from agentruntime_worker.tools import context_objects, dev_tools, mcp_leases, mcp_tool_names, skill_pins

    from agentruntime_worker.files.authority import task_attachments
    from agentruntime_worker.runtime_capabilities import from_config
    gateway = model_gateway(config)
    builder = ContextBuilder(gateway, context_objects(config), ContextConfig(effective_tokens=config.context_effective_tokens, offload_tokens=config.context_offload_tokens))
    return ToolLoopActivities(
        ModelToolLoop(
            gateway, builder, tools=ToolCatalog(dev_tools(config), mcp_leases(config)), transcript=worker_transcript(config),
            mcp_tools=lambda: mcp_tool_names(config), memory=searcher(config), runtime_capabilities=from_config(config),
            personal=TaskPersonalConfig(skill_pins(config).store) if config.registry_address else None,
            attachments=task_attachments(config),
            sub_agent_tier=SUB_AGENT_TIER if config.sub_agent_policy == "rules" else None,
        )
    )
