"""测试用的假 RegistryService：grpc.aio 在进程内起服务，按内存里的数据回答，记录收到的请求与元数据。

错误的形状照 Go 侧的约定（与 Model Gateway 相同）：gRPC 状态码 + trailer ar-error-bin（序列化的 agentruntime.common.v1.Error）。
列表接口每页最多 page_size（默认 2）条，用来检查客户端把分页取完。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import grpc
from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import enums_pb2, error_pb2, payload_pb2
from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime.registry.v1 import registry_pb2_grpc as rg_grpc

from .gateway_fakes import Abort

DEFAULT = rg.RELEASE_STATE_DEFAULT


def json_payload(data: Any) -> payload_pb2.JsonPayload:
    value = struct_pb2.Value()
    json_format.ParseDict(data, value)
    return payload_pb2.JsonPayload(value=value)


def template(name: str, version: str, executor: str, effect: str, output: str = "", state: int = DEFAULT) -> rg.NodeTemplate:
    return rg.NodeTemplate(
        name=name,
        version=version,
        state=state,
        executor=enums_pb2.Executor.Value(f"EXECUTOR_{executor.upper()}"),
        effect=enums_pb2.Effect.Value(f"EFFECT_{effect.upper()}"),
        role=enums_pb2.ROLE_TRANSFORM,
        output=enums_pb2.ModelOutput.Value(f"MODEL_OUTPUT_{output.upper()}") if output else enums_pb2.MODEL_OUTPUT_UNSPECIFIED,
    )


def recipe(name: str, version: str, family: str, risk: str, typical_cost_usd: float | None = None, hooks: dict[str, list[str]] | None = None) -> rg.Recipe:
    doc: dict[str, Any] = {
        "task_family": family,
        "description": f"{name} 的做法",
        "risk": risk,
        "stages": [{"goal": "完成", "exit_when": "完成", "nodes": [{"id": "n1", "kind": "atomic", "template": "answer@v1"}]}],
    }
    if typical_cost_usd is not None:
        doc["typical_cost_usd"] = typical_cost_usd
    if hooks is not None:
        doc["hooks"] = hooks
    return rg.Recipe(name=name, version=version, state=DEFAULT, task_family=family, document=json_payload(doc))


def model_profile(profile_id: str, klass: str, tiers: list[str], capabilities: list[str] | None = None) -> rg.ModelProfile:
    doc = {"id": profile_id, "class": klass, "tiers": [{"name": t} for t in tiers], "capabilities": capabilities or ["generate", "structured"]}
    return rg.ModelProfile(id=profile_id, version="v1", state=DEFAULT, document=json_payload(doc))


def not_found(code: str = "not_found") -> Abort:
    return Abort(grpc.StatusCode.NOT_FOUND, "没有", error_pb2.Error(category=4, code=code, message="Registry 里没有这一项"))


def unavailable() -> Abort:
    return Abort(grpc.StatusCode.UNAVAILABLE, "Registry 不可用")


@dataclass
class FakeRegistry(rg_grpc.RegistryServiceServicer):
    """templates 是 (名字, 版本) → NodeTemplate；recipes 是默认版本的 Recipe 列表。abort 不为 None 时每个调用都失败。"""

    templates: dict[tuple[str, str], rg.NodeTemplate] = field(default_factory=dict)
    recipes: list[rg.Recipe] = field(default_factory=list)
    model_profiles: list[rg.ModelProfile] = field(default_factory=list)
    # (租户, 版本) → 租户策略的正文
    policies: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    abort: Abort | None = None
    page_size: int = 2
    requests: list[tuple[str, Any, dict[str, str]]] = field(default_factory=list)

    def add(self, *templates: rg.NodeTemplate) -> None:
        for t in templates:
            self.templates[(t.name, t.version)] = t

    def calls(self, method: str) -> list[Any]:
        return [req for m, req, _ in self.requests if m == method]

    def metadata(self, method: str) -> list[dict[str, str]]:
        return [md for m, _, md in self.requests if m == method]

    async def _enter(self, method: str, request: Any, context: grpc.aio.ServicerContext) -> None:
        self.requests.append((method, request, {k: v for k, v in context.invocation_metadata() or () if isinstance(v, str)}))
        if self.abort is not None:
            await context.abort(self.abort.code, self.abort.details, self.abort.trailer())

    def _page(self, items: list[Any], token: str) -> tuple[list[Any], str]:
        start = int(token or "0")
        end = start + self.page_size
        return items[start:end], (str(end) if end < len(items) else "")

    async def GetNodeTemplate(self, request: rg.GetNodeTemplateRequest, context: grpc.aio.ServicerContext) -> rg.GetNodeTemplateResponse:  # noqa: N802
        await self._enter("GetNodeTemplate", request, context)
        found = self.templates.get((request.name, request.version))
        if found is None:
            abort = not_found("template_not_found")
            await context.abort(abort.code, abort.details, abort.trailer())
        return rg.GetNodeTemplateResponse(node_template=found)

    async def ListNodeTemplates(self, request: rg.ListNodeTemplatesRequest, context: grpc.aio.ServicerContext) -> rg.ListNodeTemplatesResponse:  # noqa: N802
        await self._enter("ListNodeTemplates", request, context)
        pins = dict(request.pinned_versions.node_templates)
        chosen = sorted(
            (t for (name, version), t in self.templates.items() if (pins.get(name) == version if pins else t.state == DEFAULT)),
            key=lambda t: t.name,
        )
        page, token = self._page(chosen, request.page_token)
        return rg.ListNodeTemplatesResponse(node_templates=page, next_page_token=token)

    async def ListRecipes(self, request: rg.ListRecipesRequest, context: grpc.aio.ServicerContext) -> rg.ListRecipesResponse:  # noqa: N802
        await self._enter("ListRecipes", request, context)
        chosen = sorted((r for r in self.recipes if not request.task_family or r.task_family == request.task_family), key=lambda r: r.name)
        page, token = self._page(chosen, request.page_token)
        return rg.ListRecipesResponse(recipes=page, next_page_token=token)

    async def ListModelProfiles(self, request: rg.ListModelProfilesRequest, context: grpc.aio.ServicerContext) -> rg.ListModelProfilesResponse:  # noqa: N802
        await self._enter("ListModelProfiles", request, context)
        page, token = self._page(sorted(self.model_profiles, key=lambda p: p.id), request.page_token)
        return rg.ListModelProfilesResponse(model_profiles=page, next_page_token=token)

    async def GetTenantPolicy(self, request: rg.GetTenantPolicyRequest, context: grpc.aio.ServicerContext) -> rg.GetTenantPolicyResponse:  # noqa: N802
        await self._enter("GetTenantPolicy", request, context)
        doc = self.policies.get((request.tenant.tenant_id, request.version))
        if doc is None:
            abort = not_found("tenant_policy_not_found")
            await context.abort(abort.code, abort.details, abort.trailer())
        return rg.GetTenantPolicyResponse(tenant_policy=rg.TenantPolicy(version=request.version, document=json_payload(doc)))

    async def GetRecipe(self, request: rg.GetRecipeRequest, context: grpc.aio.ServicerContext) -> rg.GetRecipeResponse:  # noqa: N802
        await self._enter("GetRecipe", request, context)
        found = next((r for r in self.recipes if r.name == request.name and (not request.version or r.version == request.version)), None)
        if found is None:
            abort = not_found("recipe_not_found")
            await context.abort(abort.code, abort.details, abort.trailer())
        return rg.GetRecipeResponse(recipe=found)

    async def Publish(self, request: rg.PublishRequest, context: grpc.aio.ServicerContext) -> rg.PublishResponse:  # noqa: N802
        await self._enter("Publish", request, context)
        return rg.PublishResponse(name=request.name, version="v1", state=rg.RELEASE_STATE_SHADOW)

    async def Promote(self, request: rg.PromoteRequest, context: grpc.aio.ServicerContext) -> rg.PromoteResponse:  # noqa: N802
        await self._enter("Promote", request, context)
        return rg.PromoteResponse(previous_default_version="")


@asynccontextmanager
async def serve(fake: FakeRegistry) -> AsyncIterator[str]:
    """在 127.0.0.1 上起假服务，返回地址 host:port。"""
    server = grpc.aio.server()
    rg_grpc.add_RegistryServiceServicer_to_server(fake, server)
    bound = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{bound}"
    finally:
        await server.stop(None)
