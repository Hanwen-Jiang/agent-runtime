"""S-03 验收：task、decision、model、program 四个任务队列的 Worker 启动入口（计划 S-03 交付；附录 K.5）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment

from agentruntime.common.v1 import options_pb2
from agentruntime.workflow.v1 import names_pb2

from agentruntime_worker import names
from agentruntime_worker.activities.stubs import StubActivities
from agentruntime_worker.config import WorkerConfig
from agentruntime_worker.executor.dev import dev_executor
from agentruntime_worker.worker import QUEUE_COMPONENTS, build_workers, decision_activities, parse_queues


def contract_queues() -> set[str]:
    enum = names_pb2.DESCRIPTOR.enum_types_by_name["TaskQueue"]
    return {v.GetOptions().Extensions[options_pb2.wire_name] for v in enum.values if v.number != 0}


def test_worker_serves_the_five_python_queues() -> None:
    assert set(QUEUE_COMPONENTS) == {names.TASK_QUEUE, names.DECISION_QUEUE, names.MODEL_QUEUE, names.PROGRAM_QUEUE, names.WORKSPACE_QUEUE}
    # session 是 Go 的队列，offline 属于 Learning Pipeline；其余都要有人消费。
    assert contract_queues() - set(QUEUE_COMPONENTS) == {"session", "offline"}


def test_workspace_operations_have_their_own_queue() -> None:
    """WS-READ-02：Workspace 操作的工作流与 activity 都在 workspace 队列；task、program 队列照旧登记它们，给改之前起的、
    还在跑的操作（旧池留到它们跑完）。"""
    workspace = QUEUE_COMPONENTS[names.WORKSPACE_QUEUE]
    assert workspace.workflows == (names.WORKSPACE_OPERATION_WORKFLOW,)
    assert workspace.activities == (names.WORKSPACE_OPERATION,)
    assert names.WORKSPACE_OPERATION_WORKFLOW in QUEUE_COMPONENTS[names.TASK_QUEUE].workflows
    assert names.WORKSPACE_OPERATION in QUEUE_COMPONENTS[names.PROGRAM_QUEUE].activities


def test_every_component_is_registered_on_exactly_one_queue() -> None:
    """Workspace 操作的工作流与 activity 例外：新旧两个队列都登记（见上一条）。"""
    queues = {q: c for q, c in QUEUE_COMPONENTS.items() if q != names.WORKSPACE_QUEUE}
    activities = [a for c in queues.values() for a in c.activities + c.local_activities]
    workflows = [w for c in queues.values() for w in c.workflows]
    assert sorted(activities) == sorted(set(activities))
    assert workflows == [
        names.TASK_WORKFLOW, names.SUB_AGENT_WORKFLOW, names.SUBGRAPH_WORKFLOW, names.SANDBOX_RECONCILE_WORKFLOW, names.ENV_BUILD_WORKFLOW,
        names.WORKSPACE_OPERATION_WORKFLOW,
    ]
    # 决策类的桩来自 StubActivities，执行 Node 的 activity 来自 Node Executor，两边不重复。
    stub_names = set(StubActivities(WorkerConfig()).activities())
    executor_names = set(dev_executor(WorkerConfig()).activities())
    assert not stub_names & executor_names
    # 沙箱环境表的对账（D-11）是平台级的 activity，不属于桩也不属于 Node Executor。
    assert set(activities) == stub_names | executor_names | {names.RECONCILE_SANDBOXES, names.BUILD_ENVIRONMENT, names.WORKSPACE_OPERATION}
    assert {names.ASK_HUMAN, names.CANCEL_HUMAN} <= set(QUEUE_COMPONENTS[names.DECISION_QUEUE].activities)
    assert names.RECONCILE_NODE in QUEUE_COMPONENTS[names.PROGRAM_QUEUE].activities


def test_local_activities_live_with_the_workflows() -> None:
    """local activity 在运行工作流的 Worker 上执行，必须和工作流登记在同一个队列。"""
    for queue, components in QUEUE_COMPONENTS.items():
        if components.local_activities:
            assert components.workflows, f"{queue} 上有 local activity 却没有工作流"
    assert QUEUE_COMPONENTS[names.TASK_QUEUE].local_activities == (names.RUN_HOOKS,)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("task,decision,model,program", ["task", "decision", "model", "program"]),
        ("task,decision,model,program,workspace", ["task", "decision", "model", "program", "workspace"]),
        ("model", ["model"]),
        (" decision , task ,decision", ["decision", "task"]),
    ],
)
def test_parse_queues(value: str, expected: list[str]) -> None:
    assert parse_queues(value) == expected


@pytest.mark.parametrize("value", ["", "session", "task,offline", "gpu"])
def test_parse_queues_rejects_unknown_queues(value: str) -> None:
    with pytest.raises(ValueError):
        parse_queues(value)


def test_the_model_planner_takes_over_plan_round_only_when_asked() -> None:
    """S-09：AR_PLANNER=model 时 plan_round 换成真正的 Planner，其余决策类 activity 仍是桩。"""
    from agentruntime_worker.activities.stubs import StubActivities
    from agentruntime_worker.planner.activities import PlannerActivities

    default = decision_activities(WorkerConfig())
    model = decision_activities(WorkerConfig(planner="model"))
    assert isinstance(default[names.PLAN_ROUND].__self__, StubActivities)
    assert isinstance(model[names.PLAN_ROUND].__self__, PlannerActivities)
    assert set(model) == set(default)
    assert isinstance(model[names.REVIEW_ROUND].__self__, StubActivities)


def test_the_model_reviewer_takes_over_review_and_wrap_up_only_when_asked() -> None:
    """S-10：AR_REVIEWER=model 时 review_round 与 finalize_task 换成真正的 Reviewer 与收尾写回，其余仍按原样。"""
    from agentruntime_worker.activities.stubs import StubActivities
    from agentruntime_worker.reviewer.activities import ReviewerActivities

    default = decision_activities(WorkerConfig())
    model = decision_activities(WorkerConfig(reviewer="model"))
    for name in (names.REVIEW_ROUND, names.FINALIZE_TASK):
        assert isinstance(default[name].__self__, StubActivities)
        assert isinstance(model[name].__self__, ReviewerActivities)
    assert isinstance(model[names.PLAN_ROUND].__self__, StubActivities), "Planner 与 Reviewer 各自开关"
    assert set(model) == set(default)


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_build_workers_registers_components_per_queue(temporal_env: WorkflowEnvironment) -> None:
    workers = build_workers(temporal_env.client, WorkerConfig(), [names.DECISION_QUEUE, names.TASK_QUEUE])
    assert [w.task_queue for w in workers] == [names.DECISION_QUEUE, names.TASK_QUEUE]


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
async def test_worker_client_encodes_payloads_through_claim_check(temporal_env: WorkflowEnvironment, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """S-06：Worker 的客户端带 Claim-Check（阈值取自配置，租户按工作流 memo 确定）；回放工具用同一份配置。"""
    from agentruntime_core.adapters import claim_check as cc

    from agentruntime_worker.worker import worker_data_converter

    config = WorkerConfig(data_dir=tmp_path, claim_check_threshold=4096)
    converter = worker_data_converter(config, temporal_env.client)
    codec = converter.payload_codec
    assert isinstance(codec, cc.ClaimCheckCodec)
    assert isinstance(codec.resolver, cc.WorkflowMemoResolver)
    assert codec.claim_check.threshold == 4096


def test_module_entry_point_prints_usage() -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "agentruntime_worker", "--help"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "--queues" in proc.stdout


def test_module_entry_point_rejects_unknown_queue() -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "agentruntime_worker", "--queues", "gpu"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60
    )
    assert proc.returncode == 2


async def test_tools_go_through_one_dev_tool_driver(tmp_path: Path) -> None:
    """T-10：call_tool 是 Node Executor 的（不再是回显的桩）；resolve_catalog 的工具 effect 与 Node Executor 用同一个开发用 ToolDriver；
    按配置登记平台内工具（docs/plan/tool-driver.md 第 2 节第 6 条）。"""
    from agentruntime_core.tenancy import TenantContext

    from agentruntime_worker.executor.dev import dev_executor
    from agentruntime_worker.executor.node_executor import NodeExecutor
    from agentruntime_worker.tools import DevCatalog, dev_tools

    config = WorkerConfig(data_dir=tmp_path, tool_workspace_root=tmp_path / "ws", tool_fetch_hosts=("example.com",))
    executor = dev_executor(config)
    activities = {**decision_activities(config), **executor.activities()}
    assert isinstance(activities[names.CALL_TOOL].__self__, NodeExecutor)
    assert isinstance(activities[names.RESOLVE_CATALOG].__self__, DevCatalog)
    provider = dev_tools(config)
    assert provider is dev_tools(config) and executor.deps.tools is provider, "同一进程只有一个开发用 ToolDriver"
    listed = {s.name for s in await provider.bind(TenantContext(tenant_id="t1")).list_tools()}
    # context_read 总是登记（R-03 评审 D11），其余按配置登记（deliver_file 跟着工作区目录，N-18）。
    assert listed == {"repo_read", "repo_list", "repo_grep", "repo_edit", "shell_run", "web_fetch", "context_read", "deliver_file"}
    unconfigured = {s.name for s in await dev_tools(WorkerConfig(data_dir=tmp_path / "other")).bind(TenantContext(tenant_id="t1")).list_tools()}
    assert unconfigured == {"context_read"}, "没配置就只有 context_read"


async def test_mcp_tools_are_attached_for_the_workers_lifetime(tmp_path: Path) -> None:
    import json
    import sys

    from agentruntime_core.tenancy import TenantContext

    from agentruntime_worker.tools import dev_tools, tool_sessions

    server = Path(__file__).with_name("mcp_fake_server.py")
    registration = {"servers": [{"name": "fake", "command": [sys.executable, str(server)], "tools": [{"tool": "echo", "name": "mcp_echo", "effect": "read"}]}]}
    (tmp_path / "mcp.json").write_text(json.dumps(registration), "utf-8")
    config = WorkerConfig(data_dir=tmp_path, mcp_tools_file=tmp_path / "mcp.json")
    async with tool_sessions(config) as gateway:
        tools = dev_tools(config).bind(TenantContext(tenant_id="t1"))
        assert (await tools.invoke("mcp_echo", "v1", {"text": "hi"})).output == {"text": "hi"}
    assert gateway is not None and not any(s.alive for s in gateway.sessions), "Worker 退出时关闭 MCP 会话"
