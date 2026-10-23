"""Registry 的 gRPC 客户端（设计书 3.1.2；T-08，细化见 docs/plan/registry.md）。

GrpcRegistry 经 grpcio 调用 task-service 内部端口上的 RegistryService（h2c，与 /healthz 同一端口；服务间认证留给 mTLS）。

- 读方法每个请求都带租户（TenantContext），不带发布令牌；列表把分页取完。
- Publish、Promote 带发布令牌（gRPC 元数据 authorization: Bearer …），令牌由调用方从 SecretStore 取来传入；
  它不出现在 repr 与错误里。平台作用域的发布不带租户。
- 错误还原与 Model Gateway 相同（executor.grpc_gateway.restore_error）：trailer ar-error-bin 优先，其次按 gRPC 状态码；
  一律抛 agentruntime_core.errors 的 ProviderError。
- 每次调用的 deadline 固定为 timeout_s（默认 10 秒）：Registry 只读写业务库，比调用它的 decision activity（2 分钟）短得多，
  超时是 TransientError，由 Temporal 重试。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any

import grpc
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2, tenant_pb2
from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime.registry.v1 import registry_pb2_grpc as rg_grpc
from agentruntime.workflow.v1 import task_pb2
from agentruntime_core.errors import InvalidError, TransientError

from agentruntime_worker.activities.types import Tenant
from agentruntime_worker.config import ENV_REGISTRY_ADDRESS, WorkerConfig
from agentruntime_worker.executor.grpc_gateway import restore_error

# 每次调用的默认 deadline（秒）。
DEFAULT_TIMEOUT_S = 10.0
# 列表每页取多少条（服务端上限 500）。
PAGE_SIZE = 200
# 分页最多取多少页：防止服务端的令牌出错时无限循环。
MAX_PAGES = 1000


class MissingRegistryAddressError(InvalidError, ValueError):
    """AR_REGISTRY_ADDRESS 缺失或为空（from_env 抛出）。同时是 ValueError，命令行按参数错误报出。"""


def tenant_context(tenant: Tenant) -> tenant_pb2.TenantContext:
    return tenant_pb2.TenantContext(tenant_id=tenant.tenant_id, user_id=tenant.user_id, workspace_id=tenant.workspace_id, session_id=tenant.session_id)


def pinned_versions(pinned: Mapping[str, Any] | None) -> task_pb2.PinnedVersions:
    """Task State 的 pinned（recipe、node_templates、reasoning_templates、model_profiles、tenant_policy）转回 proto。"""
    pinned = pinned or {}
    return task_pb2.PinnedVersions(
        recipe=pinned.get("recipe") or "",
        node_templates=dict(pinned.get("node_templates") or {}),
        reasoning_templates=dict(pinned.get("reasoning_templates") or {}),
        model_profiles=dict(pinned.get("model_profiles") or {}),
        tenant_policy=pinned.get("tenant_policy") or "",
    )


def json_payload(document: Mapping[str, Any]) -> payload_pb2.JsonPayload:
    value = struct_pb2.Value()
    json_format.ParseDict(dict(document), value)
    return payload_pb2.JsonPayload(value=value)


class GrpcRegistry:
    """到 RegistryService 的 gRPC 客户端。address 形如 127.0.0.1:8092。

    构造时不建连接；通道在第一次调用时按当时运行的事件循环创建（事件循环换了就重建）；aclose() 关闭通道，可以重复调用。
    """

    def __init__(self, address: str, *, publish_token: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.address = address
        self.timeout_s = timeout_s
        self._token = publish_token
        self._channel: grpc.aio.Channel | None = None
        self._stub: rg_grpc.RegistryServiceStub | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def __repr__(self) -> str:
        return f"GrpcRegistry({self.address!r})"

    def _client(self) -> rg_grpc.RegistryServiceStub:
        loop = asyncio.get_running_loop()
        if self._stub is None or self._loop is not loop:
            self._channel = grpc.aio.insecure_channel(self.address)
            self._stub = rg_grpc.RegistryServiceStub(self._channel)
            self._loop = loop
        return self._stub

    async def _call(self, method: str, request: Any, *, write: bool = False) -> Any:
        metadata = (("authorization", f"Bearer {self._token}"),) if write and self._token else None
        try:
            return await getattr(self._client(), method)(request, timeout=self.timeout_s, metadata=metadata)
        except grpc.aio.AioRpcError as exc:
            raise restore_error(exc) from None

    # --- 读 ---

    async def get_node_template(self, tenant: Tenant, name: str, version: str = "") -> rg.NodeTemplate:
        resp = await self._call("GetNodeTemplate", rg.GetNodeTemplateRequest(tenant=tenant_context(tenant), name=name, version=version))
        return resp.node_template

    async def list_node_templates(self, tenant: Tenant, pinned: Mapping[str, Any] | None) -> list[rg.NodeTemplate]:
        """按 Task 固定的版本列模板；pinned 为空（或没有 node_templates）时列默认版本。分页取完。"""
        out: list[rg.NodeTemplate] = []
        token = ""
        for _ in range(MAX_PAGES):
            resp = await self._call(
                "ListNodeTemplates",
                rg.ListNodeTemplatesRequest(tenant=tenant_context(tenant), pinned_versions=pinned_versions(pinned), page_size=PAGE_SIZE, page_token=token),
            )
            out.extend(resp.node_templates)
            token = resp.next_page_token
            if not token:
                return out
        raise TransientError("paging_runaway", "Registry 的分页没有结束")

    async def list_recipes(self, tenant: Tenant, task_family: str = "") -> list[rg.Recipe]:
        """租户看得到的默认 Recipe，可按任务族过滤。分页取完。"""
        out: list[rg.Recipe] = []
        token = ""
        for _ in range(MAX_PAGES):
            resp = await self._call(
                "ListRecipes", rg.ListRecipesRequest(tenant=tenant_context(tenant), task_family=task_family, page_size=PAGE_SIZE, page_token=token)
            )
            out.extend(resp.recipes)
            token = resp.next_page_token
            if not token:
                return out
        raise TransientError("paging_runaway", "Registry 的分页没有结束")

    async def get_tenant_policy(self, tenant: Tenant, version: str = "") -> rg.TenantPolicy:
        """租户策略；version 为空时取当前版本。"""
        resp = await self._call("GetTenantPolicy", rg.GetTenantPolicyRequest(tenant=tenant_context(tenant), version=version))
        return resp.tenant_policy

    async def get_recipe(self, tenant: Tenant, name: str, version: str = "") -> rg.Recipe:
        """Recipe；version 为空时取默认版本。"""
        resp = await self._call("GetRecipe", rg.GetRecipeRequest(tenant=tenant_context(tenant), name=name, version=version))
        return resp.recipe

    # --- 写（要发布令牌）---

    async def publish(
        self, tenant: Tenant | None, kind: int, name: str, document: Mapping[str, Any], *, change_note: str = ""
    ) -> rg.PublishResponse:
        """发布新版本；平台作用域的发布 tenant 为 None。"""
        request = rg.PublishRequest(kind=kind, name=name, document=json_payload(document), change_note=change_note)
        if tenant is not None:
            request.tenant.CopyFrom(tenant_context(tenant))
        return await self._call("Publish", request, write=True)

    async def promote(self, tenant: Tenant | None, kind: int, name: str, version: str, evaluation: Mapping[str, Any]) -> rg.PromoteResponse:
        """带评测证据（registry_evaluation.schema.json）把一个版本设为默认。"""
        request = rg.PromoteRequest(kind=kind, name=name, version=version, evaluation=json_payload(evaluation))
        if tenant is not None:
            request.tenant.CopyFrom(tenant_context(tenant))
        return await self._call("Promote", request, write=True)

    async def aclose(self) -> None:
        channel, self._channel, self._stub, self._loop = self._channel, None, None, None
        if channel is not None:
            await channel.close()


def from_env(env: Mapping[str, str] | None = None) -> GrpcRegistry:
    """按 AR_REGISTRY_ADDRESS 构造只读的客户端（没有发布令牌）；地址缺失或只有空白时抛 MissingRegistryAddressError。"""
    address = (os.environ if env is None else env).get(ENV_REGISTRY_ADDRESS, "").strip()
    if not address:
        raise MissingRegistryAddressError("missing_registry_address", f"没有设置 {ENV_REGISTRY_ADDRESS}（Registry 的 host:port）")
    return GrpcRegistry(address)


_opened: contextvars.ContextVar[list[GrpcRegistry] | None] = contextvars.ContextVar("opened_registries", default=None)


def registry_client(config: WorkerConfig) -> GrpcRegistry:
    """Worker 组装用：同一地址在 closing_registries() 里只建一个客户端，退出时统一关闭。调用方要先确认配了地址。"""
    opened = _opened.get()
    for client in opened or ():
        if client.address == config.registry_address:
            return client
    client = GrpcRegistry(config.registry_address)
    if opened is not None:
        opened.append(client)
    return client


@contextlib.asynccontextmanager
async def closing_registries() -> AsyncIterator[list[GrpcRegistry]]:
    """在这里面经 registry_client() 建的客户端都记下来，退出时（包括被取消）逐个关闭。"""
    opened: list[GrpcRegistry] = []
    token = _opened.set(opened)
    try:
        yield opened
    finally:
        _opened.reset(token)
        for client in opened:
            await client.aclose()
