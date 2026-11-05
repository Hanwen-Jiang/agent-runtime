"""Worker 一侧的沙箱测试共用的装配（SBX-01）：一个真的 Sandbox Service 进程（sandbox_service_support）加上 Worker 这边的
ServiceBroker、平台工具与 Workspace 操作。

服务与 Worker 共用业务库（服务数据目录下的 SQLite）、对象存储目录与工作区根，同部署里两者挂同一个卷、连同一个库。
选后端、配额、复用、存档与恢复、对账都在服务里，它们的细节由 services/go-sandbox-service 的测试覆盖；这里看 Worker 用得对：
工具的结果与 span、Workspace 操作的结果、Worker 自己写的记录（工作区、联网授权）。

服务的时钟是真的：要“空闲到期被回收”时用 destroy_session（存档模式下先存档再销毁，同对账回收），不拨时钟。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import error_pb2, tenant_pb2
from agentruntime.workflow.v1 import workspace_pb2
from agentruntime_core.dev.object_store import LocalDiskObjectStoreProvider
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.dev.tool_driver import LocalToolDriverProvider
from agentruntime_core.sealing import seal
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.sandbox.client import ServiceBroker
from agentruntime_worker.sandbox.grants import GrantStore
from agentruntime_worker.sandbox.policy import TenantPolicies
from agentruntime_worker.tools.platform import PlatformToolsConfig, register_platform_tools
from agentruntime_worker.workspace import WorkspaceArchiver
from agentruntime_worker.workspace.operations import WorkspaceOperations

from .sandbox_service_support import WORKER_TOKEN, SandboxService

S1 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"
S2 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VX"
KEY = b"k" * 32
SECRET = "pw-SECRET-1234567890"
PY = ["python", "-c"]
LOCAL = {"kind": "local", "isolation": "none"}
ARCHIVE = {"kind": "local", "isolation": "none", "workspace": "archive"}
# 环境凭据的密钥（envcred，SecretStore 平台作用域的 sandbox/env-credential-key；两边都按原样的字节用）：服务签发命令的
# 包镜像与代理凭据，Worker 签发预览地址的令牌。
ENV_CREDENTIAL_KEY = "env-credential-key-0123456789abcdef"


class Setup:
    """mode 是 mount（Worker 挂着工作区卷）或 archive（存档模式的后端，Worker 不挂卷）；specs 给了就用它登记后端。"""

    def __init__(
        self, tmp_path: Path, *, mode: str = "mount", specs: Mapping[str, Any] | None = None, default: str = "", sessions: bool = True,
        policies: Mapping[str, Mapping[str, Any]] | None = None, env: Mapping[str, str] | None = None, network_requests: bool = False,
        preview_domain: str = "", runtime: str = "python", secrets: Mapping[str, Any] | None = None, worker_mount: bool | None = None,
        hosted_backends: bool = False,
    ) -> None:
        backends = dict(specs or {"local": ARCHIVE if mode == "archive" else LOCAL})
        self.svc = SandboxService(tmp_path / "svc", {"default": default or sorted(backends)[0], "backends": backends}, policies=policies, env=env)
        doc = json.loads(self.svc.secrets_file.read_text("utf-8"))
        doc["scopes"]["_platform"]["secrets"]["sandbox/env-credential-key"] = ENV_CREDENTIAL_KEY
        for scope, values in (secrets or {}).items():
            doc["scopes"].setdefault(scope, {"secrets": {}})["secrets"].update(values)
        self.svc.secrets_file.write_text(json.dumps(doc), "utf-8")
        self.svc.start()
        self.host = self.svc.workspaces
        mounted = all(b.get("workspace", "mount") != "archive" for b in backends.values()) if worker_mount is None else worker_mount
        self.root = self.host if mounted else None
        self.store = SQLiteRelationalStoreProvider(self.svc.data / "relational")
        self.objects = LocalDiskObjectStoreProvider(self.svc.objects, signing_key=os.urandom(32))
        self.archiver = WorkspaceArchiver(self.store, self.objects)
        self.broker = ServiceBroker.connect(self.svc.address, WORKER_TOKEN, archiver=self.archiver)
        self.grants = GrantStore(self.store)
        self._policies: dict[str, Mapping[str, Any]] = {k: dict(v) for k, v in (policies or {}).items()}

        async def lookup(tenant_id: str) -> Mapping[str, Any] | None:
            return self._policies.get(tenant_id)

        self.policies = TenantPolicies(lookup, cache_ttl=0)
        self.telemetry = ConsoleTelemetryProvider(stream=io.StringIO())
        self.tools = LocalToolDriverProvider()

        async def credential_key() -> bytes | None:
            return ENV_CREDENTIAL_KEY.encode()

        self.names = register_platform_tools(
            self.tools,
            PlatformToolsConfig(workspace_root=self.root, shell_sandbox="sandbox", session_sandboxes=sessions, workspace_tools=True,
                                network_requests=network_requests, hosted_backends=hosted_backends, preview_domain=preview_domain,
                                shell_runtime=runtime),
            sandboxes=self.broker, telemetry=self.telemetry, grants=self.grants, policies=self.policies, credential_key=credential_key,
        )

        async def secret() -> bytes:
            return KEY

        self.ops = WorkspaceOperations(
            self.broker, secret=secret, allow_local_remotes=True, grants=self.grants, policies=self.policies, workspace_root=self.root,
            telemetry=self.telemetry, runtime=runtime,
        )

    def set_policy(self, tenant_id: str, policy: Mapping[str, Any]) -> None:
        """改一个租户的策略：服务（开发用的策略文件，不缓存）与 Worker 这边一起改。"""
        self._policies[tenant_id] = dict(policy)
        self.svc.write_policies(self._policies)

    # --- 工具 ---

    @staticmethod
    def tenant(tenant_id: str = "t1", session: str = S1, workspace: str = "w1", user: str = "u1") -> TenantContext:
        return TenantContext(tenant_id=tenant_id, user_id=user, workspace_id=workspace, session_id=session)

    async def call(self, tool: str, args: dict[str, Any], *, task_id: str = "", **kw: Any) -> dict[str, Any]:
        tenant = self.tenant(**kw)
        if self.root is not None:
            (self.root / tenant.tenant_id / (tenant.workspace_id or "default")).mkdir(parents=True, exist_ok=True)
        extra = {"task_id": task_id} if task_id else {}
        out = (await self.tools.bind(tenant).invoke(tool, "v1", args, idempotency_key=f"idk-{os.urandom(4).hex()}", **extra)).output
        await self.telemetry.bind(tenant).flush()
        return out

    async def shell(self, argv: list[str], session: str = S1, workspace: str = "w1", **extra: Any) -> dict[str, Any]:
        return await self.call("shell_run", {"argv": argv, **extra}, session=session, workspace=workspace)

    async def py(self, code: str, **kw: Any) -> dict[str, Any]:
        return await self.call("shell_run", {"argv": [*PY, code]}, **kw)

    def spans(self, name: str = "runtime.sandbox") -> list[dict[str, Any]]:
        return [dict(s.attributes) for s in self.telemetry.exported_spans if s.name == name]

    # --- Workspace 操作 ---

    @staticmethod
    def inp(op: int, workspace: str = "w1", tenant: str = "t1", sealed_for: tuple[str, str] | None = None, **args: Any) -> workspace_pb2.WorkspaceOperationInput:
        operation_id = "wso-" + uuid.uuid4().hex[:26]
        msg = workspace_pb2.WorkspaceOperationInput(
            tenant=tenant_pb2.TenantContext(tenant_id=tenant, user_id="u-alice"), operation_id=operation_id, operation=op, workspace_id=workspace,
            requested_by="u-alice",
        )
        if args:
            msg.args.CopyFrom(json_format.ParseDict(args, struct_pb2.Struct()))
        if sealed_for is not None:
            for_tenant, for_op = sealed_for
            msg.sealed_credential = seal(KEY, for_tenant, for_op or operation_id, json.dumps({"username": "bot", "password": SECRET}).encode())
        return msg

    async def run(self, op: int, **kw: Any) -> dict[str, Any]:
        out = await self.ops.run(self.inp(op, **kw))
        if out.HasField("error"):
            raise AssertionError(f"{out.error.code}: {out.error.message}")
        return json_format.MessageToDict(out.result)

    async def error(self, op: int, **kw: Any) -> error_pb2.Error:
        out = await self.ops.run(self.inp(op, **kw))
        assert out.HasField("error"), out
        return out.error

    async def tool(self, name: str, args: dict[str, Any], workspace: str = "w1") -> dict[str, Any]:
        return await self.call(name, args, workspace=workspace)

    # --- 服务的环境表（读服务的业务库；行级的细节由 Go 的测试覆盖） ---

    def envs(self, owner: str = "t1", *, active: bool = True) -> list[dict[str, Any]]:
        path = self.svc.data / "relational" / f"{owner}.sqlite3"
        if not path.exists():
            return []
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in db.execute("SELECT * FROM sandbox_envs ORDER BY created_at")]
            except sqlite3.OperationalError:
                return []
        if active:
            rows = [r for r in rows if r["state"] in ("creating", "ready", "destroying")]
        return rows
