"""Node Executor 的 activity（设计书 3.2.4；附录 D、K.4；计划 S-05）。

不可绕过的规则，每次执行都在这里检查，不依赖工作流是否检查过：

- effect 为 write_* 的 Node 必须带幂等键，且等于 keys.idempotency_key 按 tenant_id、task_id、node_id 与输入重新算出的值；
  缺少或对不上（例如模型自己写的）一律拒绝（denied）。
- write_irreversible 的 Node 必须带通过的审批记录：gate 为 <id>.1、选项为 approve，否则拒绝（审批双保险）。
- program 节点调用的工具，effect 不能高于 Node 声明的 effect。
- 只缓存 effect 为 none 或 read、且声明了 cache 的 Node；缓存键 = 模板版本 + 输入哈希，按租户隔离，TTL 取 cache.ttl_s。
- 结果按 output_schema 校验：model 节点不合格时带上校验错误重问一次，仍不合格为 invalid_output；其他执行者直接 invalid_output。
- 结果超过 ExecutorConfig.inline_result_limit 字节时写入 ObjectStore（路径带租户前缀），result 只留摘要与引用，
  meta.result_ref 记录 uri、大小与 sha256。
- rationale 只写入 trace（Telemetry 的 span 事件），不放进 activity 的结果，不进事件历史，不传给下游。
- program 节点要求权限的工具，用 SecretStore 签发的短期凭据（角色 tool.<工具名>），调用结束即吊销；凭据不进日志与 trace。
  工具的输出或错误信息带出凭据原文时换成掩码（span 事件 credential_redacted）；吊销失败记 span 事件与警告日志，等租约过期。
- 写操作超时、结果未知时先按幂等键对账：已生效就取当时的结果；没生效时 write_reversible 可重试（同一幂等键），
  write_irreversible 记为结果未知（unknown），交给 Reviewer，不重试。对账用的查询本身暂时不可用时，不可逆写操作
  报 reconcile，由工作流用 reconcile_node 带重试地再查（计划 S-07 场景 3）。
- 写操作重试（attempt > 1，例如 Worker 崩溃后被接手）时，工具支持按幂等键查询就先查：外部已生效就取当时的结果，
  不再执行；确认没有生效才执行（设计书 3.6“恢复”）。进程内的幂等记录随 Worker 崩溃丢失，只有外部的记录可信。

失败一律以 ApplicationError 抛出，type 是下列之一，non_retryable 由这里决定（附录 D.6）：
  transient（可重试；Node 写了 retry.on 时只重试其中的原因）、invalid_output、invalid、conflict、denied、unknown、internal；
  另有 reconcile：结果未知、这次没能查询，交给工作流对账。
执行期间后台持续发心跳，Worker 崩溃后 activity 能被及时接手。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from google.protobuf import json_format, struct_pb2
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentruntime.common.v1 import enums_pb2, payload_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError, ProviderError, TransientError
from agentruntime_core.providers.cache import CacheProvider
from agentruntime_core.providers.human_channel import HumanChannelProvider, Question
from agentruntime_core.providers.object_store import ObjectStoreProvider
from agentruntime_core.providers.sandbox import SandboxProvider, SandboxSpec
from agentruntime_core.providers.secret_store import Credential, SecretStoreProvider
from agentruntime_core.providers.telemetry import Span, TelemetryProvider
from agentruntime_core.providers.tool_driver import ToolDriver, ToolDriverProvider, ToolSpec
from agentruntime_core.secrets import MASK
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker import names
from agentruntime_worker.activities.types import HumanCancel, HumanQuestion, NodeInput, ReconcileResult, Tenant, ToolCallInput
from agentruntime_worker.executor import keys
from agentruntime_worker.executor.model_gateway import ModelGateway
from agentruntime_worker.executor.schemas import OutputSchemaResolver, UnknownSchema, schema_for, validate
from agentruntime_worker.tools.catalog import latest_spec

log = logging.getLogger(__name__)

# ApplicationError.type 的取值。
TRANSIENT = "transient"
INVALID_OUTPUT = "invalid_output"
INVALID = "invalid"
CONFLICT = "conflict"
DENIED = "denied"
UNKNOWN = "unknown"
INTERNAL = "internal"
# 写操作结果未知，且这次没能按幂等键查询：工作流接着用 reconcile_node 对账。
RECONCILE = "reconcile"
ERROR_TYPES = (TRANSIENT, INVALID_OUTPUT, INVALID, CONFLICT, DENIED, UNKNOWN, INTERNAL, RECONCILE)

EFFECT_ORDER = ("none", "read", "write_reversible", "write_irreversible")
WRITE_EFFECTS = ("write_reversible", "write_irreversible")
CACHEABLE_EFFECTS = ("none", "read")
# 声明了 cache 但没写 ttl_s 时的缓存时长。
DEFAULT_CACHE_TTL = {"none": timedelta(hours=1), "read": timedelta(minutes=5)}
SYSTEM_PROMPT = "你在 Agent Runtime 里执行一个 Atomic Node。按 Node 的模板与输入完成任务；需要结构化输出时严格按给定的 JSON Schema。"


@dataclass(frozen=True)
class ExecutorConfig:
    # 结果的 JSON 超过这么多字节就写入 ObjectStore（计划 S-05 难点“大结果”）。
    inline_result_limit: int = 64 * 1024
    # 外置结果的摘要保留多少个字符。
    preview_chars: int = 1024
    # 短期凭据的最长有效期。
    credential_ttl: timedelta = timedelta(minutes=15)
    # 心跳间隔的上限；实际取 heartbeat 超时的 1/3。
    heartbeat_interval: timedelta = timedelta(seconds=5)


@dataclass
class ExecutorDeps:
    tools: ToolDriverProvider
    objects: ObjectStoreProvider
    secrets: SecretStoreProvider
    cache: CacheProvider
    humans: HumanChannelProvider
    telemetry: TelemetryProvider
    models: ModelGateway
    schemas: OutputSchemaResolver
    sandbox: SandboxProvider | None = None


def _rank(effect: str) -> int:
    return EFFECT_ORDER.index(effect) if effect in EFFECT_ORDER else len(EFFECT_ORDER)


def channel_question_id(task_id: str, round_seq: int, question_id: str) -> str:
    """HumanChannel 里的问题 id：同一租户内唯一（问题按 id 幂等），所以带上 Task 与 Round。
    工作流等的是 Node id（question_id），回复送回时由 workflow_question_id 还原。"""
    return f"{task_id}/r{round_seq}/{question_id}"


def workflow_question_id(channel_id: str) -> str:
    """channel_question_id 的逆：取最后一段（Node id 不含 /）。"""
    return channel_id.rsplit("/", 1)[-1]


def _tenant(t: Tenant) -> TenantContext:
    return TenantContext(tenant_id=t.tenant_id, user_id=t.user_id, workspace_id=t.workspace_id, session_id=t.session_id)


def _split_template(template: str) -> tuple[str, str]:
    name, _, version = template.partition("@")
    if not name or not version:
        raise ApplicationError(f"template 应形如 name@v1，实际 {template!r}", type=INVALID, non_retryable=True)
    return name, version


def _error(message: str, error_type: str, *, retryable: bool = False) -> ApplicationError:
    return ApplicationError(message, type=error_type, non_retryable=not retryable)


def _plaintext(credential: Credential | None) -> str:
    if credential is None:
        return ""
    value = credential.secret.reveal()
    return value.decode("utf-8", "ignore") if isinstance(value, bytes) else value


def _masked(value: Any, plaintext: str) -> Any:
    if isinstance(value, str):
        return value.replace(plaintext, MASK)
    if isinstance(value, Mapping):
        return {_masked(k, plaintext): _masked(v, plaintext) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_masked(v, plaintext) for v in value]
    return value


def _redact_output(output: Any, credential: Credential | None, tool: str, span: Span) -> Any:
    """工具输出里出现这次签发的凭据原文时换成掩码（R-04）：结果会进事件历史、trace、下游与 Reviewer。"""
    plaintext = _plaintext(credential)
    if not plaintext or plaintext not in json.dumps(output, ensure_ascii=False, default=str):
        return output
    assert credential is not None
    span.add_event("credential_redacted", {"tool": tool, "lease_id": credential.lease_id, "where": "output"})
    return _masked(output, plaintext)


def _redact_error(err: ProviderError, credential: Credential | None, tool: str, span: Span) -> ProviderError:
    """工具错误的说明里出现凭据原文时换成掩码，类别、code 与 retry_after 不变。"""
    plaintext = _plaintext(credential)
    if not plaintext or plaintext not in err.message:
        return err
    assert credential is not None
    span.add_event("credential_redacted", {"tool": tool, "lease_id": credential.lease_id, "where": "error"})
    return type(err)(err.code, err.message.replace(plaintext, MASK), retry_after=err.retry_after)


def _enum(enum: Any, prefix: str, value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(enum.Value(prefix + value.upper()))
    except ValueError:
        return 0


def _to_value(obj: Any) -> struct_pb2.Value:
    value = struct_pb2.Value()
    json_format.ParseDict(obj, value)
    return value


class NodeExecutor:
    def __init__(self, deps: ExecutorDeps, config: ExecutorConfig | None = None) -> None:
        self.deps = deps
        self.config = config or ExecutorConfig()

    # --- activity ---

    @activity.defn(name=names.EXECUTE_MODEL_NODE)
    async def execute_model_node(self, inp: NodeInput) -> dict[str, Any]:
        """执行 model 节点，返回 Node Output（不含 rationale）。"""
        return await self._execute(inp, self._model)

    @activity.defn(name=names.CALL_TOOL)
    async def call_tool(self, inp: ToolCallInput) -> Any:
        """tool_loop 的一次工具调用（T-10）：按名字取当前登记的最大版本，当作这次调用的 program 节点执行，规则同 program 节点。

        - 没登记：denied。工具登记的 effect 高于工作流批准的（inp.effect，展开时取自 Catalog）：denied，不调用（_tool 里的检查）。
        - 写操作的幂等键按 tenant_id、task_id、这次调用的 id 与参数由代码生成；write_irreversible 要这次调用自己的审批记录。
        - 凭据、写操作超时的对账、大结果外置都同 program 节点。返回工具的输出（外置时是摘要与引用）。
        """
        from agentruntime_worker.executor.tool_gate import require_tool_gate  # tool_gate 引用本模块的 DENIED

        require_tool_gate(inp)
        tenant = _tenant(inp.tenant)
        try:
            spec = await latest_spec(self.deps.tools.bind(tenant), inp.tool)
        except ProviderError as err:
            raise self._from_provider(err, {}) from None
        if spec is None:
            raise _error(f"工具 {inp.tool} 没有登记，不能调用", DENIED)
        node: dict[str, Any] = {
            "id": inp.node_id,
            "kind": "atomic",
            "template": f"{spec.name}@{spec.version}",
            "executor": "program",
            "effect": inp.effect,
            "inputs": dict(inp.args),
        }
        if inp.effect in WRITE_EFFECTS:
            node["idempotency_key"] = keys.idempotency_key(inp.tenant.tenant_id, inp.task_id, inp.node_id, node["inputs"])
        out = await self._execute(NodeInput(tenant=inp.tenant, task_id=inp.task_id, round_seq=inp.round_seq, node=node, gate=inp.gate), self._program)
        return out["result"]

    @activity.defn(name=names.EXECUTE_PROGRAM_NODE)
    async def execute_program_node(self, inp: NodeInput) -> dict[str, Any]:
        """执行 program 节点：template 的名字与版本就是 ToolDriver 里的工具；内置模板 sandbox_exec 在 Sandbox 里执行代码。"""
        return await self._execute(inp, self._program)

    @activity.defn(name=names.ASK_HUMAN)
    async def ask_human(self, q: HumanQuestion) -> None:
        """经 HumanChannel 发出问题，立即返回；回复以信号送回 Task 工作流。"""
        channel = self.deps.humans.bind(_tenant(q.tenant))
        try:
            await channel.ask(
                Question(
                    question_id=channel_question_id(q.task_id, q.round_seq, q.question_id),
                    task_id=q.task_id,
                    text=q.text,
                    options=tuple(q.options),
                    deadline=datetime.fromisoformat(q.deadline),
                    state_summary=q.state_summary,
                )
            )
        except ProviderError as err:
            raise self._from_provider(err, {}) from None

    @activity.defn(name=names.CANCEL_HUMAN)
    async def cancel_human(self, c: HumanCancel) -> None:
        """撤回问题。通道里已经没有这个问题（例如已清理）时没有可撤回的，按已撤回处理。"""
        try:
            await self.deps.humans.bind(_tenant(c.tenant)).cancel(channel_question_id(c.task_id, c.round_seq, c.question_id))
        except NotFoundError:
            return
        except ProviderError as err:
            raise self._from_provider(err, {}) from None

    @activity.defn(name=names.RECONCILE_NODE)
    async def reconcile_node(self, inp: NodeInput) -> ReconcileResult:
        """写操作结果未知（例如 activity 超时）时，按幂等键向外部查询是否已经生效（附录 K.4）。只查询，不执行。"""
        node = inp.node
        tenant = _tenant(inp.tenant)
        name, version = _split_template(node["template"])
        key = node.get("idempotency_key") or keys.idempotency_key(inp.tenant.tenant_id, inp.task_id, node["id"], node.get("inputs", {}))
        try:
            driver = self.deps.tools.bind(tenant)
            spec = await driver.describe(name, version)
            if not spec.supports_idempotency_lookup:
                return ReconcileResult(found=False, reason=f"工具 {name} 不支持按幂等键查询，结果未知")
            found = await driver.lookup(name, version, key)
        except ProviderError as err:
            raise self._from_provider(err, node) from None
        if found is None:
            return ReconcileResult(found=False, reason="按幂等键查不到，外部没有生效的记录")
        output = await self._externalize(tenant, inp, {"result": found.output, "meta": {}})
        return ReconcileResult(found=True, output=output)

    def activities(self) -> dict[str, Any]:
        return {
            names.EXECUTE_MODEL_NODE: self.execute_model_node,
            names.EXECUTE_PROGRAM_NODE: self.execute_program_node,
            names.ASK_HUMAN: self.ask_human,
            names.CANCEL_HUMAN: self.cancel_human,
            names.RECONCILE_NODE: self.reconcile_node,
            names.CALL_TOOL: self.call_tool,
        }

    # --- 公共流程 ---

    async def _execute(self, inp: NodeInput, body: Any) -> dict[str, Any]:
        node = inp.node
        tenant = _tenant(inp.tenant)
        telemetry = self.deps.telemetry.bind(tenant)
        attributes = {
            "node_id": node.get("id", ""),
            "task_id": inp.task_id,
            "template": node.get("template", ""),
            "executor": node.get("executor", ""),
            "effect": node.get("effect", ""),
        }
        try:
            async with self._heartbeating(), telemetry.start_span("node.execute", attributes=attributes) as span:
                try:
                    self._check_rules(inp)
                    cached = await self._cache_get(tenant, node)
                    if cached is not None:
                        span.add_event("cache_hit")
                        return cached
                    out = await body(inp, tenant, span)
                    out = await self._externalize(tenant, inp, out)
                    await self._cache_put(tenant, node, out)
                    return out
                except (ApplicationError, asyncio.CancelledError):
                    raise
                except ProviderError as err:
                    raise self._from_provider(err, node) from None
                except Exception as err:  # noqa: BLE001 — 未预料的错误一律不可重试，交给 Reviewer
                    raise _error(f"执行 {node.get('id')} 时出错：{type(err).__name__}", INTERNAL) from None
        finally:
            # 导出失败不影响 Node 的结局（Telemetry 语义：只在 flush 体现，且只能是 transient）。
            with contextlib.suppress(ProviderError):
                await telemetry.flush()

    @contextlib.asynccontextmanager
    async def _heartbeating(self) -> AsyncIterator[None]:
        """执行期间后台持续发心跳，间隔取 heartbeat 超时的 1/3（不超过 ExecutorConfig.heartbeat_interval）。"""
        timeout = activity.info().heartbeat_timeout if activity.in_activity() else None
        interval = self.config.heartbeat_interval.total_seconds()
        if timeout:
            interval = min(interval, timeout.total_seconds() / 3)

        async def beat() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(interval)

        task = asyncio.create_task(beat()) if activity.in_activity() else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def _check_rules(self, inp: NodeInput) -> None:
        node = inp.node
        effect = str(node.get("effect", "none"))
        if effect in WRITE_EFFECTS:
            expected = keys.idempotency_key(inp.tenant.tenant_id, inp.task_id, node["id"], node.get("inputs", {}))
            if node.get("idempotency_key") != expected:
                raise _error(f"写操作 {node['id']} 缺少由代码生成的幂等键", DENIED)
        if effect == "write_irreversible":
            gate = inp.gate
            if gate is None or gate.option != "approve" or gate.gate_node != f"{node['id']}.1":
                raise _error(f"write_irreversible 的 {node['id']} 没有通过的人工审批记录", DENIED)

    def _from_provider(self, err: ProviderError, node: Mapping[str, Any]) -> ApplicationError:
        message = f"{err.code}：{err.message}" if err.message else err.code
        if isinstance(err, TransientError):
            retry_on = (node.get("retry") or {}).get("on")
            return _error(message, TRANSIENT, retryable=not retry_on or err.code in retry_on)
        if isinstance(err, InvalidError):
            return _error(message, INVALID_OUTPUT if err.code == "invalid_output" else INVALID)
        if isinstance(err, DeniedError):
            return _error(message, DENIED)
        if isinstance(err, NotFoundError):
            return _error(message, INVALID)
        if isinstance(err, ConflictError):
            return _error(message, CONFLICT)
        return _error(message, INTERNAL)

    def _schema(self, node: Mapping[str, Any]) -> Mapping[str, Any] | None:
        try:
            return schema_for(node, self.deps.schemas)
        except UnknownSchema as err:
            raise _error(str(err), INVALID) from None

    # --- 缓存 ---

    def _cacheable(self, node: Mapping[str, Any]) -> bool:
        return bool(node.get("cache")) and node.get("effect") in CACHEABLE_EFFECTS

    async def _cache_get(self, tenant: TenantContext, node: Mapping[str, Any]) -> dict[str, Any] | None:
        if not self._cacheable(node):
            return None
        try:
            raw = await self.deps.cache.bind(tenant).get(keys.cache_key(node["template"], node.get("inputs", {})))
        except ProviderError:
            return None  # 缓存只存可丢失的数据，读不到就当没命中。
        return json.loads(raw) if raw is not None else None

    async def _cache_put(self, tenant: TenantContext, node: Mapping[str, Any], out: dict[str, Any]) -> None:
        if not self._cacheable(node):
            return
        ttl_s = (node.get("cache") or {}).get("ttl_s")
        ttl = timedelta(seconds=int(ttl_s)) if ttl_s else DEFAULT_CACHE_TTL[str(node["effect"])]
        with contextlib.suppress(ProviderError):
            await self.deps.cache.bind(tenant).set(keys.cache_key(node["template"], node.get("inputs", {})), keys.canonical_json(out).encode("utf-8"), ttl=ttl)

    # --- 大结果 ---

    async def _externalize(self, tenant: TenantContext, inp: NodeInput, out: dict[str, Any]) -> dict[str, Any]:
        data = keys.canonical_json(out.get("result")).encode("utf-8")
        if len(data) <= self.config.inline_result_limit:
            return out
        digest = hashlib.sha256(data).hexdigest()
        key = f"node-results/{inp.task_id}/{inp.node['id']}-{digest[:16]}.json"
        info = await self.deps.objects.bind(tenant).put(key, data, content_type="application/json")
        meta = {**out.get("meta", {}), "result_ref": {"uri": info.uri, "size_bytes": len(data), "sha256": info.sha256}}
        summary = data.decode("utf-8")[: self.config.preview_chars]
        return {**out, "result": {"summary": summary, "ref": info.uri, "size_bytes": len(data)}, "meta": meta}

    # --- program 节点 ---

    async def _program(self, inp: NodeInput, tenant: TenantContext, span: Span) -> dict[str, Any]:
        node = inp.node
        name, version = _split_template(str(node["template"]))
        schema = self._schema(node)
        started = time.monotonic()
        if name == names.SANDBOX_TEMPLATE:
            output = await self._sandbox(node, tenant)
        else:
            output = await self._tool(node, tenant, name, version, span)
        if schema is not None:
            errors = validate(schema, output)
            if errors:
                raise _error(f"{node['id']} 的结果不符合 output_schema：" + "；".join(errors), INVALID_OUTPUT)
        return {"result": output, "meta": {"latency_ms": int((time.monotonic() - started) * 1000)}}

    async def _tool(self, node: Mapping[str, Any], tenant: TenantContext, name: str, version: str, span: Span) -> Any:
        driver = self.deps.tools.bind(tenant)
        spec = await driver.describe(name, version)
        effect = str(node.get("effect", "none"))
        if _rank(spec.effect.value) > _rank(effect):
            raise _error(f"工具 {name} 的 effect {spec.effect.value} 高于 {node['id']} 声明的 {effect}", DENIED)
        key = node.get("idempotency_key") if effect in WRITE_EFFECTS else None
        if key and spec.supports_idempotency_lookup and activity.in_activity() and activity.info().attempt > 1:
            # 重试：上一次 attempt 可能已经在外部生效（Worker 崩溃时结果没写入历史），先查再决定是否执行。
            found = await driver.lookup(name, version, key)
            if found is not None:
                span.add_event("reconciled_before_retry")
                span.set_attributes({"replayed": True})
                return found.output
        secrets = None
        credential = None
        if spec.permissions:
            secrets = self.deps.secrets.bind(tenant)
            ttl = min(self.config.credential_ttl, spec.timeout + timedelta(minutes=1))
            credential = await secrets.issue_credential(f"tool.{name}", ttl=ttl)
        try:
            try:
                result = await driver.invoke(name, version, node.get("inputs", {}), idempotency_key=key, credential=credential)
                span.set_attributes({"replayed": result.replayed})
                return _redact_output(result.output, credential, name, span)
            except TransientError as err:
                if effect not in WRITE_EFFECTS:
                    raise
                found = await self._reconcile_timeout(driver, spec, node, key or "", _redact_error(err, credential, name, span))
                return _redact_output(found, credential, name, span)
        except ProviderError as err:
            raise _redact_error(err, credential, name, span) from None
        finally:
            if credential is not None and secrets is not None:
                try:
                    await secrets.revoke(credential.lease_id)
                except ProviderError as err:
                    # 没吊销成功的租约只能等过期：记进 trace 与日志，事后查得到；Node 的结局不受影响。
                    span.add_event("credential_revoke_failed", {"tool": name, "lease_id": credential.lease_id, "code": err.code})
                    log.warning("吊销工具 %s 的短期凭据失败（租约 %s，%s），等它过期", name, credential.lease_id, err.code)

    async def _reconcile_timeout(self, driver: ToolDriver, spec: ToolSpec, node: Mapping[str, Any], key: str, err: TransientError) -> Any:
        """写操作超时、结果未知：先按幂等键对账，不直接重试（附录 K.4）。"""
        reversible = node.get("effect") == "write_reversible"
        if spec.supports_idempotency_lookup:
            try:
                found = await driver.lookup(spec.name, spec.version, key)
            except TransientError as lookup_err:
                if reversible:
                    # 重试时先查（attempt > 1），查到就不再执行。
                    raise self._from_provider(lookup_err, node) from None
                raise _error(f"{node['id']} 写操作超时，按幂等键查询暂时不可用（{lookup_err.code}），交给对账", RECONCILE) from None
            if found is not None:
                return found.output
            if reversible:
                raise self._from_provider(err, node)
            raise _error(f"{node['id']} 写操作超时，按幂等键查不到生效记录，结果未知", UNKNOWN)
        if reversible:
            # 同一幂等键重试，工具负责去重。
            raise self._from_provider(err, node)
        raise _error(f"{node['id']} 写操作超时，工具 {spec.name} 不支持按幂等键查询，结果未知", UNKNOWN)

    async def _sandbox(self, node: Mapping[str, Any], tenant: TenantContext) -> dict[str, Any]:
        if self.deps.sandbox is None:
            raise _error("没有配置 Sandbox，不能执行 sandbox_exec", INVALID)
        inputs = node.get("inputs", {})
        runtime = str(inputs.get("runtime") or "python")
        code = inputs.get("code")
        if not isinstance(code, str):
            raise _error("sandbox_exec 的输入必须有字符串 code", INVALID)
        timeout = timedelta(seconds=int((node.get("budget") or {}).get("timeout_s") or 60))
        box = self.deps.sandbox.bind(tenant)
        env = await box.create(SandboxSpec(runtime=runtime, network="none"))
        try:
            res = await box.exec(env.env_id, [runtime, "-c", code], timeout=timeout, stdin=str(inputs.get("stdin", "")).encode("utf-8"))
        finally:
            with contextlib.suppress(ProviderError):
                await box.destroy(env.env_id)
        return {
            "exit_code": res.exit_code,
            "stdout": res.stdout.decode("utf-8", errors="replace"),
            "stderr": res.stderr.decode("utf-8", errors="replace"),
            "timed_out": res.timed_out,
        }

    # --- model 节点 ---

    def _call(self, inp: NodeInput) -> mg.CallContext:
        node = inp.node
        return mg.CallContext(
            task_id=inp.task_id,
            node_id=node["id"],
            role=_enum(enums_pb2.Role, "ROLE_", node.get("role")),
            tier=str(node.get("tier") or ""),
            reasoning=_enum(enums_pb2.Reasoning, "REASONING_", node.get("reasoning")),
        )

    def _messages(self, inp: NodeInput, retry_errors: list[str] | None) -> list[mg.ChatMessage]:
        """S-05 的最小上下文：系统提示加 inject 的内容，用户消息是模板与输入的 JSON。Context Builder 接入后由它组装。"""
        node = inp.node
        system = SYSTEM_PROMPT
        for injection in inp.injections:
            system += f"\n[{injection.get('zone', '')}] {injection.get('content', '')}"
        messages = [
            mg.ChatMessage(role=mg.CHAT_ROLE_SYSTEM, text=system),
            mg.ChatMessage(role=mg.CHAT_ROLE_USER, text=keys.canonical_json({"template": node.get("template"), "inputs": node.get("inputs", {})})),
        ]
        if retry_errors:
            messages.append(
                mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="上一次的输出不符合 output_schema，请改正后重新输出：\n" + "\n".join(retry_errors))
            )
        return messages

    async def _generate(self, inp: NodeInput, tenant: TenantContext, schema: Mapping[str, Any] | None, retry_errors: list[str] | None) -> mg.GenerateResponse:
        params = mg.GenerateParams(messages=self._messages(inp, retry_errors))
        if schema is not None:
            params.output_schema.CopyFrom(payload_pb2.JsonPayload(value=_to_value(dict(schema))))
        return await self.deps.models.generate(mg.GenerateRequest(tenant=tenant, call=self._call(inp), generation=params))

    async def _model(self, inp: NodeInput, tenant: TenantContext, span: Span) -> dict[str, Any]:
        node = inp.node
        started = time.monotonic()
        if node.get("output") == "embed":
            embedded = await self.deps.models.embed(
                mg.EmbedRequest(tenant=tenant, call=self._call(inp), texts=[keys.canonical_json(node.get("inputs", {}))])
            )
            return {"result": {"embeddings": [list(e.values) for e in embedded.embeddings]}, "meta": self._meta(node, embedded.model, embedded.usage, started)}
        schema = self._schema(node)
        response = await self._generate(inp, tenant, schema, None)
        result = self._result_of(response)
        errors = validate(schema, result) if schema is not None else []
        if errors:
            # 输出不符合 schema：带上校验错误重问一次（附录 D.3、D.6）。
            span.add_event("invalid_output_retry", {"errors": "；".join(errors)})
            response = await self._generate(inp, tenant, schema, errors)
            result = self._result_of(response)
            errors = validate(schema, result) if schema is not None else []
            if errors:
                raise _error(f"{node['id']} 的输出重问一次后仍不符合 output_schema：" + "；".join(errors), INVALID_OUTPUT)
        if response.rationale:
            span.add_event("rationale", {"text": response.rationale})
        return {"result": result, "meta": self._meta(node, response.model, response.usage, started)}

    @staticmethod
    def _result_of(response: mg.GenerateResponse) -> Any:
        if response.HasField("result"):
            return json_format.MessageToDict(response.result)
        return {"text": response.text}

    @staticmethod
    def _meta(node: Mapping[str, Any], model: mg.ModelChoice, usage: Any, started: float) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "tokens": {"input": int(usage.input_tokens), "output": int(usage.output_tokens), "cache_read": int(usage.cache_read_tokens)},
            "cost_usd": usage.cost_usd_micros / 1_000_000,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        tier = model.tier or node.get("tier")
        if tier:
            meta["tier"] = tier
        if node.get("reasoning"):
            meta["reasoning"] = node["reasoning"]
        return meta
