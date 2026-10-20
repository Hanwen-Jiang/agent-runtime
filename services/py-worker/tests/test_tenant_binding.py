"""S-12a 验收“接口层强制租户上下文”（Worker 一侧）：Node Executor 用到的每个 Provider 都按 Node 所属 Task 的租户绑定。

- 租户只来自工作流的输入（NodeInput.tenant，源头是认证身份），不来自 Node 的输入：输入里写别的租户也改不了绑定。
- 执行 Node 时不经平台作用域访问租户数据（没有 bind_platform）。
- 模型调用带同一个租户上下文（Model Gateway 按租户限流、计费、选档）。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from agentruntime_worker import names
from agentruntime_worker.activities.types import HumanCancel, HumanQuestion
from agentruntime_worker.executor.node_executor import ExecutorConfig, ExecutorDeps, NodeExecutor
from agentruntime_worker.executor.schemas import DictSchemaResolver

from .executor_harness import REGISTERED_SCHEMAS, TASK_ID, TENANT, make_fixture, model, node_input, program


class Spy:
    """Provider 工厂的包装：记下每次绑定用的作用域。"""

    def __init__(self, name: str, inner: Any, log: list[tuple[str, ...]]) -> None:
        self.name, self.inner, self.log = name, inner, log

    def bind(self, tenant: Any) -> Any:
        self.log.append((self.name, "tenant", tenant.tenant_id, tenant.user_id, tenant.session_id))
        return self.inner.bind(tenant)

    def bind_platform(self, purpose: str) -> Any:
        self.log.append((self.name, "platform", purpose))
        return self.inner.bind_platform(purpose)

    async def aclose(self) -> None:
        await self.inner.aclose()


class SpyGateway:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.tenants: list[str] = []

    async def generate(self, request: Any) -> Any:
        self.tenants.append(request.tenant.tenant_id)
        return await self.inner.generate(request)

    async def embed(self, request: Any) -> Any:
        self.tenants.append(request.tenant.tenant_id)
        return await self.inner.embed(request)


def env_for(name: str, queue: str) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info, activity_type=name, task_queue=queue, workflow_id=TASK_ID, heartbeat_timeout=timedelta(seconds=2), start_to_close_timeout=timedelta(seconds=30)
    )
    return env


@pytest.mark.asyncio
async def test_every_provider_is_bound_to_the_tasks_tenant(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    log: list[tuple[str, ...]] = []
    gateway = SpyGateway(fx.models)
    deps = ExecutorDeps(
        tools=Spy("tools", fx.tools, log),
        objects=Spy("objects", fx.objects, log),
        secrets=Spy("secrets", fx.secrets, log),
        cache=Spy("cache", fx.cache, log),
        humans=Spy("humans", fx.humans, log),
        telemetry=Spy("telemetry", fx.telemetry, log),
        models=gateway,
        schemas=DictSchemaResolver(REGISTERED_SCHEMAS),
        sandbox=Spy("sandbox", fx.sandbox, log),
    )
    executor = NodeExecutor(deps, ExecutorConfig(inline_result_limit=200))
    fx.tools.grant("t1", "crm.read")
    fx.secrets.grant_role("t1", "tool.crm_read")

    async def big(_: Any) -> Any:
        return {"rows": [{"region": f"r{i}"} for i in range(40)]}

    fx.behaviors["lookup_rate"] = big
    fx.models.reply({"text": "回答"})
    program_env = env_for(names.EXECUTE_PROGRAM_NODE, names.PROGRAM_QUEUE)
    decision_env = env_for(names.ASK_HUMAN, names.DECISION_QUEUE)

    # Node 的输入冒充别的租户，改不了绑定。
    forged = {"tenant_id": "t2", "tenant": {"tenant_id": "t2"}, "q": 1}
    await program_env.run(executor.execute_program_node, node_input(program("q", "crm_read", inputs=forged)))
    assert fx.invocations[-1].tenant_id == "t1", "工具拿到的租户来自工作流的输入，不来自 Node 的输入"
    await program_env.run(executor.execute_program_node, node_input(program("big", "lookup_rate")))
    await program_env.run(executor.execute_program_node, node_input(program("cached", "compute", effect="none", cache={"ttl_s": 60})))
    await program_env.run(executor.execute_program_node, node_input(program("calc", names.SANDBOX_TEMPLATE, effect="none", inputs={"code": "print(1)"})))
    await env_for(names.EXECUTE_MODEL_NODE, names.MODEL_QUEUE).run(executor.execute_model_node, node_input(model("m")))
    await env_for(names.RECONCILE_NODE, names.PROGRAM_QUEUE).run(executor.reconcile_node, node_input(program("w", "update_crm", effect="write_reversible")))
    deadline = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await decision_env.run(executor.ask_human, HumanQuestion(tenant=TENANT, task_id=TASK_ID, question_id="confirm", text="继续吗？", options=["approve"], deadline=deadline))
    await decision_env.run(executor.cancel_human, HumanCancel(tenant=TENANT, task_id=TASK_ID, question_id="confirm"))

    assert [e for e in log if e[1] == "platform"] == [], "执行 Node 时不经平台作用域访问数据"
    wrong = [e for e in log if e[1:] != ("tenant", TENANT.tenant_id, TENANT.user_id, TENANT.session_id)]
    assert not wrong, f"每次绑定都用 Task 的租户上下文：{wrong}"
    assert {e[0] for e in log} == {"tools", "objects", "secrets", "cache", "humans", "telemetry", "sandbox"}, "每个用到的 Provider 都核对过"
    assert gateway.tenants == ["t1"], "模型调用带 Task 的租户"
