"""本地函数 ToolDriver（开发实现）。语义见 agentruntime_core.providers.tool_driver。

工具是进程内的 async 函数，用 register 登记；权限用 grant 授予。幂等记录只在内存里，进程重启就没了。
工具背后是外部系统、而外部系统能按幂等键查询时，登记时给 lookup：lookup 交给外部系统回答，
进程重启之后也能对账（附录 K.4）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from agentruntime_core.dev._common import PLATFORM_ID, scope_id
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError, NotFoundError, TransientError
from agentruntime_core.providers.secret_store import Credential
from agentruntime_core.providers.tool_driver import ToolHandler, ToolInvocation, ToolResult, ToolSpec
from agentruntime_core.tenancy import Scope, TenantContext, platform_scope, tenant_scope
from agentruntime_core.testing import FaultInjector

_NAME = re.compile(r"[a-z][a-z0-9_.-]*")
_VERSION = re.compile(r"v[0-9]+")
# 平台工具登记在这里，对所有作用域可见。
_GLOBAL = "*"


@dataclass(frozen=True)
class ToolLookup:
    """交给工具自己的 lookup 的一次查询。tenant_id 来自客户端的作用域（平台作用域为空串）。"""

    tenant_id: str
    idempotency_key: str


# 按幂等键查外部系统：生效过返回当时的输出，没有返回 None。
ToolLookupHandler = Callable[[ToolLookup], Awaitable[Any | None]]


@dataclass
class _Tool:
    spec: ToolSpec
    handler: ToolHandler
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator
    lookup: ToolLookupHandler | None = None


@dataclass(frozen=True)
class _Record:
    arguments_hash: str
    output: Any


def _hash(arguments: Mapping[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# invalid_arguments 的说明（TOOL-INT-FLOAT）：写出哪个参数、违反了什么，模型看了能自己改对。参数是模型写的，可能带着它看到的
# 凭据，所以说明里的东西只取自 schema（参数名、可选值、类型、上下限），参数的值与 schema 没定义的参数名一律不回显。
_MESSAGE_PREFIX = "参数不符合工具的 input_schema"
_MESSAGE_LIMIT = 400
_SHOWN_LIMIT = 40
_ENUM_LIMIT = 10
_COUNTS = {
    "minimum": "不能小于 {}",
    "maximum": "不能大于 {}",
    "exclusiveMinimum": "要大于 {}",
    "exclusiveMaximum": "要小于 {}",
    "multipleOf": "要是 {} 的倍数",
    "minLength": "至少 {} 个字符",
    "maxLength": "最多 {} 个字符",
    "minItems": "至少 {} 项",
    "maxItems": "最多 {} 项",
    "minProperties": "至少 {} 个字段",
    "maxProperties": "最多 {} 个字段",
}


def _shown(value: Any) -> str:
    """schema 里的一个值（可选值、上下限、格式），截短。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _SHOWN_LIMIT else text[: _SHOWN_LIMIT - 1] + "…"


def _where(error: ValidationError) -> str:
    """出错的参数位置：数组下标照写；字段名只写 schema 里声明过的（在 schema 路径上出现），别的写成 …。"""
    declared = {str(part) for part in error.absolute_schema_path}
    return "/".join(str(part) if isinstance(part, int) or str(part) in declared else "…" for part in error.absolute_path)


def _describe(error: ValidationError) -> str:
    where, kind, limit = _where(error), str(error.validator), error.validator_value
    subject = where or "参数"
    if kind == "required" and isinstance(limit, list):
        present = error.instance if isinstance(error.instance, Mapping) else {}
        missing = "、".join(str(name) for name in limit if name not in present)
        return f"{where} 缺少必填的 {missing}" if where else f"缺少必填参数 {missing}"
    if kind == "additionalProperties":
        allowed = "、".join(sorted(str(name) for name in (error.schema.get("properties") or {})))
        text = f"{where} 里有 schema 没定义的参数" if where else "有 schema 没定义的参数"
        return f"{text}（可用的：{allowed}）" if allowed else text
    if kind == "enum" and isinstance(limit, list):
        values = "、".join(_shown(value) for value in limit[:_ENUM_LIMIT]) + (" 等" if len(limit) > _ENUM_LIMIT else "")
        return f"{subject} 只能是 {values} 之一"
    if kind == "const":
        return f"{subject} 只能是 {_shown(limit)}"
    if kind == "type":
        types = limit if isinstance(limit, list) else [limit]
        return f"{subject} 的类型应为 {' 或 '.join(_shown(t) for t in types)}"
    if kind in _COUNTS:
        return f"{subject} {_COUNTS[kind].format(_shown(limit))}"
    if kind == "uniqueItems":
        return f"{subject} 里不能有重复的项"
    if kind == "pattern":
        return f"{subject} 的格式不对（要匹配 {_shown(limit)}）"
    if kind == "format":
        return f"{subject} 的格式应为 {_shown(limit)}"
    return f"{subject} 不满足 schema 的 {kind}"


def _argument_error(validator: Draft202012Validator, arguments: Mapping[str, Any]) -> InvalidError | None:
    """参数合格时为 None；不合格时说明第一处（按位置排序，结果稳定），其余只计数。"""
    errors = sorted(validator.iter_errors(arguments), key=lambda e: ([str(p) for p in e.absolute_path], str(e.validator)))
    if not errors:
        return None
    rest = f"（另有 {len(errors) - 1} 处不符合）" if len(errors) > 1 else ""
    message = f"{_MESSAGE_PREFIX}：{_describe(errors[0])}"
    if len(message) + len(rest) > _MESSAGE_LIMIT:
        message = message[: _MESSAGE_LIMIT - len(rest) - 1] + "…"
    return InvalidError("invalid_arguments", message + rest)


class LocalToolDriverProvider:
    def __init__(self, *, faults: FaultInjector | None = None) -> None:
        self._faults = faults or FaultInjector()
        # 登记范围（_GLOBAL 或作用域）→ (名字, 版本) → 工具
        self._tools: dict[str, dict[tuple[str, str], _Tool]] = {}
        self._grants: dict[str, set[str]] = {}
        self._records: dict[tuple[str, str, str, str], _Record] = {}
        self._key_locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}

    def bind(self, tenant: TenantContext) -> LocalToolDriver:
        return LocalToolDriver(self, tenant_scope(tenant))

    def bind_platform(self, purpose: str) -> LocalToolDriver:
        return LocalToolDriver(self, platform_scope(purpose))

    async def aclose(self) -> None:
        return None

    # --- 开发专用：登记与授权 ---

    def register(
        self, spec: ToolSpec, handler: ToolHandler, *, tenant_id: str | None = None, lookup: ToolLookupHandler | None = None
    ) -> None:
        """登记工具。lookup 只能配给声明了 supports_idempotency_lookup 的工具。"""
        if not _NAME.fullmatch(spec.name) or not _VERSION.fullmatch(spec.version):
            raise InvalidError("invalid_tool", "工具名或版本格式不对")
        if lookup is not None and not spec.supports_idempotency_lookup:
            raise InvalidError("invalid_tool", "没有声明支持按幂等键查询的工具不能配 lookup")
        try:
            input_validator = Draft202012Validator(dict(spec.input_schema))
            output_validator = Draft202012Validator(dict(spec.output_schema))
            input_validator.check_schema(dict(spec.input_schema))
            output_validator.check_schema(dict(spec.output_schema))
        except SchemaError as error:
            raise InvalidError("invalid_schema", "工具的 JSON Schema 不合法") from error
        owner = tenant_id or _GLOBAL
        self._tools.setdefault(owner, {})[(spec.name, spec.version)] = _Tool(spec, handler, input_validator, output_validator, lookup)

    def grant(self, tenant_id: str | None, permission: str) -> None:
        self._grants.setdefault(tenant_id or PLATFORM_ID, set()).add(permission)


class LocalToolDriver:
    def __init__(self, provider: LocalToolDriverProvider, scope: Scope) -> None:
        self._provider = provider
        self._scope = scope
        self._owner = scope_id(scope)

    def _visible(self) -> dict[tuple[str, str], _Tool]:
        return {**self._provider._tools.get(_GLOBAL, {}), **self._provider._tools.get(self._owner, {})}

    def _tool(self, name: str, version: str) -> _Tool:
        tool = self._visible().get((name, version))
        if tool is None:
            raise NotFoundError("unknown_tool", "工具未登记，或版本不存在")
        return tool

    async def list_tools(self) -> list[ToolSpec]:
        self._provider._faults.check("list_tools")
        return [tool.spec for _, tool in sorted(self._visible().items())]

    async def describe(self, name: str, version: str) -> ToolSpec:
        self._provider._faults.check("describe")
        return self._tool(name, version).spec

    async def invoke(
        self,
        name: str,
        version: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        credential: Credential | None = None,
        task_id: str = "",
        approved_by: str = "",
    ) -> ToolResult:
        self._provider._faults.check("invoke")
        tool = self._tool(name, version)
        missing = tool.spec.permissions - self._provider._grants.get(self._owner, set())
        if missing:
            raise DeniedError("permission_denied", "工具要求的权限没有授予本作用域")
        arguments = dict(arguments)
        invalid = _argument_error(tool.input_validator, arguments)
        if invalid is not None:
            raise invalid
        if tool.spec.effect.is_write and not idempotency_key:
            raise InvalidError("missing_idempotency_key", "写操作必须带幂等键")
        if not idempotency_key:
            return ToolResult(output=await self._run(tool, arguments, None, credential, task_id, approved_by), idempotency_key=None, replayed=False)

        record_key = (self._owner, name, version, idempotency_key)
        arguments_hash = _hash(arguments)
        lock = self._provider._key_locks.setdefault(record_key, asyncio.Lock())
        async with lock:
            record = self._provider._records.get(record_key)
            if record is not None:
                if record.arguments_hash != arguments_hash:
                    raise ConflictError("idempotency_conflict", "同一幂等键配了不同的参数")
                return ToolResult(output=record.output, idempotency_key=idempotency_key, replayed=True)
            output = await self._run(tool, arguments, idempotency_key, credential, task_id, approved_by)
            self._provider._records[record_key] = _Record(arguments_hash, output)
            return ToolResult(output=output, idempotency_key=idempotency_key, replayed=False)

    async def _run(
        self, tool: _Tool, arguments: dict[str, Any], key: str | None, credential: Credential | None, task_id: str = "", approved_by: str = ""
    ) -> Any:
        invocation = ToolInvocation(
            tenant_id=self._scope.tenant_id,
            arguments=arguments,
            idempotency_key=key,
            credential=credential,
            workspace_id=self._scope.workspace_id,
            session_id=self._scope.session_id,
            user_id=self._scope.user_id,
            task_id=task_id,
            approved_by=approved_by,
        )
        try:
            output = await asyncio.wait_for(tool.handler(invocation), timeout=tool.spec.timeout.total_seconds())
        except TimeoutError as error:
            raise TransientError("timeout", "工具调用超时") from error
        if next(tool.output_validator.iter_errors(output), None) is not None:
            raise InvalidError("invalid_output", "工具返回不符合 output_schema")
        return output

    async def lookup(self, name: str, version: str, idempotency_key: str) -> ToolResult | None:
        self._provider._faults.check("lookup")
        tool = self._tool(name, version)
        if not tool.spec.supports_idempotency_lookup:
            raise InvalidError("lookup_unsupported", "该工具不支持按幂等键查询")
        record = self._provider._records.get((self._owner, name, version, idempotency_key))
        if record is not None:
            return ToolResult(output=record.output, idempotency_key=idempotency_key, replayed=True)
        if tool.lookup is None:
            return None
        try:
            output = await asyncio.wait_for(
                tool.lookup(ToolLookup(tenant_id=self._scope.tenant_id, idempotency_key=idempotency_key)), timeout=tool.spec.timeout.total_seconds()
            )
        except TimeoutError as error:
            raise TransientError("timeout", "按幂等键查询超时") from error
        if output is None:
            return None
        if next(tool.output_validator.iter_errors(output), None) is not None:
            raise InvalidError("invalid_output", "工具返回不符合 output_schema")
        return ToolResult(output=output, idempotency_key=idempotency_key, replayed=True)
