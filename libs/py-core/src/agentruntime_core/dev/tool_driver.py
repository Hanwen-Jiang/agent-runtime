"""本地函数 ToolDriver（开发实现）。语义见 agentruntime_core.providers.tool_driver。

工具是进程内的 async 函数，用 register 登记；权限用 grant 授予。幂等记录只在内存里。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

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


@dataclass
class _Tool:
    spec: ToolSpec
    handler: ToolHandler
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator


@dataclass(frozen=True)
class _Record:
    arguments_hash: str
    output: Any


def _hash(arguments: Mapping[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


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

    def register(self, spec: ToolSpec, handler: ToolHandler, *, tenant_id: str | None = None) -> None:
        if not _NAME.fullmatch(spec.name) or not _VERSION.fullmatch(spec.version):
            raise InvalidError("invalid_tool", "工具名或版本格式不对")
        try:
            input_validator = Draft202012Validator(dict(spec.input_schema))
            output_validator = Draft202012Validator(dict(spec.output_schema))
            input_validator.check_schema(dict(spec.input_schema))
            output_validator.check_schema(dict(spec.output_schema))
        except SchemaError as error:
            raise InvalidError("invalid_schema", "工具的 JSON Schema 不合法") from error
        owner = tenant_id or _GLOBAL
        self._tools.setdefault(owner, {})[(spec.name, spec.version)] = _Tool(spec, handler, input_validator, output_validator)

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
    ) -> ToolResult:
        self._provider._faults.check("invoke")
        tool = self._tool(name, version)
        missing = tool.spec.permissions - self._provider._grants.get(self._owner, set())
        if missing:
            raise DeniedError("permission_denied", "工具要求的权限没有授予本作用域")
        arguments = dict(arguments)
        if next(tool.input_validator.iter_errors(arguments), None) is not None:
            raise InvalidError("invalid_arguments", "参数不符合工具的 input_schema")
        if tool.spec.effect.is_write and not idempotency_key:
            raise InvalidError("missing_idempotency_key", "写操作必须带幂等键")
        if not idempotency_key:
            return ToolResult(output=await self._run(tool, arguments, None, credential), idempotency_key=None, replayed=False)

        record_key = (self._owner, name, version, idempotency_key)
        arguments_hash = _hash(arguments)
        lock = self._provider._key_locks.setdefault(record_key, asyncio.Lock())
        async with lock:
            record = self._provider._records.get(record_key)
            if record is not None:
                if record.arguments_hash != arguments_hash:
                    raise ConflictError("idempotency_conflict", "同一幂等键配了不同的参数")
                return ToolResult(output=record.output, idempotency_key=idempotency_key, replayed=True)
            output = await self._run(tool, arguments, idempotency_key, credential)
            self._provider._records[record_key] = _Record(arguments_hash, output)
            return ToolResult(output=output, idempotency_key=idempotency_key, replayed=False)

    async def _run(self, tool: _Tool, arguments: dict[str, Any], key: str | None, credential: Credential | None) -> Any:
        invocation = ToolInvocation(
            tenant_id=self._scope.tenant_id, arguments=arguments, idempotency_key=key, credential=credential
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
        if record is None:
            return None
        return ToolResult(output=record.output, idempotency_key=idempotency_key, replayed=True)
