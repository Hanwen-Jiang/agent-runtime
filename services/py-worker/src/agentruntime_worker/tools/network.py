"""request_network：运行中的 Task 申请联网（设计书改动稿 W8；docs/plan/cloud-deploy.md D-16）。

- 对模型公开的是 read 级控制申请；无论 Task 的读写上限，每次调用前都经独立 HumanChannel
  人工审批（D16），审批的问题里列出主机、端口、时长与理由；批准之后才执行。Node Executor 把审批人交给工具（approved_by）。
- 按租户策略的 sandbox.network 核对（sandbox.grants.check_request）：不允许、主机不在范围、时长超过上限一律 denied 并说明原因。
  approver 是 admin 时，提交者本人批的不算（人工回复只接受提交者与带 task.approve 的人，所以别人批的就是管理员批的）。
- 批准的申请写进 sandbox_grants，就是 gate 记录：出网网关在它的主机、端口、时限与用量上限之内放行，不再逐次审批。
  Task 结束、到期或被撤销即收回。
- preview_port：给本会话的环境开一个预览地址（进网，W8）。要有会话级的环境（会话级沙箱打开）与能开预览的后端（K8s、本地）；
  地址形如 https://<环境号>.<预览域名>/?ar_preview=<令牌>，令牌是环境凭据（用途 preview，绑定环境与端口，到期同授权），
  像预签名地址一样给到用户或回调方；授权收回即失效（网关按授权核对）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from agentruntime_core import envcred
from agentruntime_core.enums import Effect
from agentruntime_core.errors import DeniedError, InvalidError
from agentruntime_core.providers.sandbox import SandboxSpec
from agentruntime_core.providers.tool_driver import ToolHandler, ToolInvocation, ToolSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.client import ServiceBroker
from agentruntime_worker.sandbox.grants import GRANT_MINUTES, MAX_HOSTS, GrantStore, NetworkPolicy, check_request
from agentruntime_worker.sandbox.policy import TenantPolicies
from agentruntime_worker.sandbox.times import parse_iso, utc_now

NAME = "request_network"
VERSION = "v1"


class NetworkTool:
    def __init__(
        self, broker: ServiceBroker, grants: GrantStore, policies: TenantPolicies, credential_key: Callable[[], Awaitable[bytes | None]], *,
        preview_domain: str = "", runtime: str = "python", session_sandboxes: bool = False,
    ) -> None:
        self.broker = broker
        self.grants = grants
        self.policies = policies
        # 预览地址的令牌是环境凭据（用途 preview）：签发的密钥是 SecretStore 平台作用域的 sandbox/env-credential-key。
        self.credential_key = credential_key
        self.preview_domain = preview_domain.strip(".").lower()
        self.runtime = runtime
        self.session_sandboxes = session_sandboxes

    def tool(self) -> tuple[ToolSpec, ToolHandler]:
        schema = {
            "type": "object",
            "required": ["minutes", "reason"],
            "additionalProperties": False,
            "properties": {
                "scope": {"type": "string", "enum": ["hosts", "public"], "description": "hosts：指定主机（默认）；public：所有公网域名，hosts 必须为空，需要租户允许 open"},
                "hosts": {
                    "type": "array", "minItems": 0, "maxItems": MAX_HOSTS, "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 253},
                    "description": "要访问的主机名（可以 *. 开头表示子域名），不能是 IP 地址，不带端口与路径",
                },
                "ports": {"type": "array", "maxItems": 10, "items": {"type": "integer", "minimum": 1, "maximum": 65535}, "description": "端口；不写为 80 与 443"},
                "minutes": {"type": "integer", "enum": list(GRANT_MINUTES), "description": "15/30/60 分钟，或 0 表示本任务不限时（须租户允许，任务结束/撤销即失效）"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 500, "description": "为什么要联网，给批准的人看"},
                "preview_port": {"type": "integer", "minimum": 1, "maximum": 65535, "description": "要从外网访问沙箱的这个端口时填（回调、预览）"},
            },
        }
        spec = ToolSpec(
            name=NAME,
            version=VERSION,
            description="运行中申请本任务联网权限。先暂停等待用户批准；可选分钟时长或本任务不限时，任务结束/撤销即失效。批准后仅在租户允许的主机/公网范围内生效，拒绝不会联网。",
            input_schema=schema,
            output_schema={"type": "object"},
            effect=Effect.READ,  # permission control; StageRunner + activity enforce a separate human gate
            permissions=frozenset(),
            timeout=timedelta(seconds=120),
        )
        return spec, self.request

    async def request(self, invocation: ToolInvocation) -> Any:
        tenant_id, task_id = invocation.tenant_id, invocation.task_id
        if not tenant_id:
            raise DeniedError("no_tenant", "request_network 只在租户作用域里可用")
        if not task_id:
            raise InvalidError("no_task", "联网授权属于 Task，request_network 要在 Task 里调用")
        args = invocation.arguments
        policy = NetworkPolicy.from_policy((await self.policies.policy_for(tenant_id)).network)
        try:
            request = check_request(
                policy, scope=str(args.get("scope", "hosts")), hosts=list(args.get("hosts") or ()), ports=list(args.get("ports") or ()), minutes=int(args["minutes"]), reason=str(args.get("reason") or ""),
                preview_port=int(args.get("preview_port") or 0),
            )
        except DeniedError as err:
            return {"status": "requires_admin", "code": err.code, "reason": err.message,
                    "allowed_hosts": list(policy.hosts), "allowed_modes": list(policy.modes), "granted": False}
        if not invocation.approved_by:
            raise DeniedError("network_not_approved", "联网要有人批准（request_network 只能经审批调用）")
        if policy.approver == "admin" and invocation.approved_by == invocation.user_id:
            return {"status": "requires_admin", "code": "network_needs_admin", "reason": "租户策略要求管理员批准联网", "granted": False}
        # 授权记在 Sandbox Service 的业务库里（服务按它给命令配代理、给托管后端换放行名单）：先问一次服务，开发时的 SQLite 里这个
        # 租户的表由服务建好（结构变更归服务，SBX-01）。
        await self.broker.describe(tenant_id)
        preview_env = preview_target = ""
        if request.preview_port:
            preview_env, preview_target = await self._preview_target(invocation, request.preview_port)
        grant = await self.grants.create(
            tenant_id, request, policy, task_id=task_id, approved_by=invocation.approved_by, workspace_id=invocation.workspace_id,
            session_id=invocation.session_id, preview_env=preview_env, preview_target=preview_target,
        )
        out: dict[str, Any] = {
            "status": "granted", "granted": True, "unlimited": request.minutes == 0, "minutes": request.minutes,
            "grant_id": grant.grant_id, "scope": grant.scope, "hosts": list(grant.hosts), "ports": list(grant.ports), "expires_at": grant.expires_at,
            "note": "shell_run 里的命令经 HTTP(S)_PROXY 访问这些主机；到期或 Task 结束即收回",
        }
        if preview_env:
            out["preview_url"] = await self._preview_url(tenant_id, preview_env, request.preview_port, grant.expires_at)
        return out

    async def _preview_target(self, invocation: ToolInvocation, port: int) -> tuple[str, str]:
        """本会话的环境（没有就建一个）与预览转发到的地址。"""
        if not self.preview_domain:
            raise InvalidError("preview_unsupported", "这个部署没有配预览地址的域名（AR_PREVIEW_DOMAIN）")
        if not (self.session_sandboxes and invocation.session_id):
            raise InvalidError("preview_unsupported", "预览地址要有会话级的沙箱环境（每条命令一个环境时，环境跑完就没了）")
        tenant = TenantContext(
            tenant_id=invocation.tenant_id, workspace_id=invocation.workspace_id or "default", session_id=invocation.session_id, user_id=invocation.user_id
        )
        spec = SandboxSpec(runtime=self.runtime, workspace_id=tenant.workspace_id, session_id=invocation.session_id, idle_timeout=timedelta(minutes=15))
        lease = await self.broker.acquire(tenant, spec, task_id=invocation.task_id, writable=True, reuse=True)
        try:
            if lease.record is None:
                raise InvalidError("preview_unsupported", f"沙箱后端 {lease.backend.name} 不能开预览地址")
            target = await lease.box.preview_address(lease.env_id, port)
            return lease.record.env_ref, target
        finally:
            await self.broker.release(lease, keep=True)

    async def _preview_url(self, tenant_id: str, env_ref: str, port: int, expires_at: str) -> str:
        key = await self.credential_key()
        if not key:
            raise InvalidError("preview_unsupported", f"SecretStore 的平台作用域里没有 {envcred.SECRET_NAME}")
        expires = parse_iso(expires_at)
        assert expires is not None
        expires = min(expires, utc_now() + timedelta(hours=6))
        token = envcred.issue(key, envcred.Claims(env_ref=env_ref, tenant_id=tenant_id, expires_at=int(expires.timestamp()), purpose="preview", limits={"port": port}))
        return f"https://{env_ref}.{self.preview_domain}/?ar_preview={token}"


__all__ = ["NAME", "NetworkTool"]
