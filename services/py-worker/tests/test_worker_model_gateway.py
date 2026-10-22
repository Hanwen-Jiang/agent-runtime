"""Model Gateway 单供应商直连（docs/plan/model-gateway-direct.md 交付 3）：Worker 的接线。

设了 AR_MODEL_GATEWAY_ADDRESS 时，Node Executor、Planner（AR_PLANNER=model）、Reviewer（AR_REVIEWER=model）
的模型调用都用 GrpcModelGateway；没设时仍是桩模型 StubModelGateway（端到端测试的默认不变）。
断言走 Worker 组装依赖的入口（executor.dev.dev_executor、worker.decision_activities、planner.activities.dev_planner、
reviewer.activities.dev_reviewer），不碰核心链路。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import WorkflowEnvironment

from agentruntime_worker import names
from agentruntime_worker import worker as worker_module
from agentruntime_worker.activities.stubs import StubModelGateway
from agentruntime_worker.config import ENV_MODEL_GATEWAY_ADDRESS, WorkerConfig
from agentruntime_worker.executor.dev import dev_executor
from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway
from agentruntime_worker.planner.activities import dev_planner
from agentruntime_worker.reviewer.activities import dev_reviewer
from agentruntime_worker.worker import decision_activities

from .gateway_fakes import FakeModelGateway, embed_request, generate_request, generate_response, serve

ADDRESS = "127.0.0.1:8091"


def test_env_variable_name() -> None:
    assert ENV_MODEL_GATEWAY_ADDRESS == "AR_MODEL_GATEWAY_ADDRESS"


def test_config_reads_the_model_gateway_address() -> None:
    assert WorkerConfig().model_gateway_address == ""
    assert WorkerConfig.from_env({}).model_gateway_address == "", "默认为空：用桩模型"
    assert WorkerConfig.from_env({ENV_MODEL_GATEWAY_ADDRESS: ADDRESS}).model_gateway_address == ADDRESS
    assert WorkerConfig.from_env({ENV_MODEL_GATEWAY_ADDRESS: f" {ADDRESS} "}).model_gateway_address == ADDRESS


def _config(tmp_path: Path, address: str = "") -> WorkerConfig:
    return WorkerConfig(data_dir=tmp_path / "data", planner="model", reviewer="model", model_gateway_address=address)


def test_without_an_address_everything_uses_the_stub_model(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert isinstance(dev_executor(config).deps.models, StubModelGateway)
    assert isinstance(dev_planner(config).planner.gateway, StubModelGateway)
    assert isinstance(dev_reviewer(config).reviewer.gateway, StubModelGateway)
    activities = decision_activities(config)
    assert isinstance(activities[names.PLAN_ROUND].__self__.planner.gateway, StubModelGateway)
    assert isinstance(activities[names.REVIEW_ROUND].__self__.reviewer.gateway, StubModelGateway)


def test_with_an_address_executor_planner_and_reviewer_use_the_grpc_gateway(tmp_path: Path) -> None:
    """交付 3：三处模型调用都换成 GrpcModelGateway。在事件循环之外组装（Worker 组装依赖时可能还没有事件循环）。"""
    config = _config(tmp_path, ADDRESS)
    assert isinstance(dev_executor(config).deps.models, GrpcModelGateway), "Node Executor"
    assert isinstance(dev_planner(config).planner.gateway, GrpcModelGateway), "Planner"
    assert isinstance(dev_reviewer(config).reviewer.gateway, GrpcModelGateway), "Reviewer"
    activities = decision_activities(config)
    assert isinstance(activities[names.PLAN_ROUND].__self__.planner.gateway, GrpcModelGateway)
    assert isinstance(activities[names.REVIEW_ROUND].__self__.reviewer.gateway, GrpcModelGateway)


def test_stub_planner_and_reviewer_stay_stubs_with_an_address(tmp_path: Path) -> None:
    """AR_PLANNER、AR_REVIEWER 仍各自开关：设了网关地址也不会把桩 Planner、桩 Reviewer 换掉。"""
    from agentruntime_worker.activities.stubs import StubActivities

    config = WorkerConfig(data_dir=tmp_path / "data", model_gateway_address=ADDRESS)
    activities = decision_activities(config)
    for name in (names.PLAN_ROUND, names.REVIEW_ROUND, names.FINALIZE_TASK):
        assert isinstance(activities[name].__self__, StubActivities)
    assert isinstance(dev_executor(config).deps.models, GrpcModelGateway), "model 节点仍经网关"


@pytest.mark.parametrize("component", ["executor", "planner", "reviewer"])
async def test_wired_gateway_calls_the_configured_address(tmp_path: Path, component: str) -> None:
    """接上的客户端确实连到 AR_MODEL_GATEWAY_ADDRESS 给的地址。"""
    fake = FakeModelGateway()
    async with serve(fake) as address:
        config = _config(tmp_path, address)
        if component == "executor":
            gateway = dev_executor(config).deps.models
        elif component == "planner":
            gateway = dev_planner(config).planner.gateway
        else:
            gateway = dev_reviewer(config).reviewer.gateway
        assert isinstance(gateway, GrpcModelGateway)
        try:
            response = await gateway.generate(generate_request())
            await gateway.embed(embed_request())
        finally:
            await gateway.aclose()
    assert response == generate_response()
    assert [type(r).__name__ for r in fake.requests] == ["GenerateRequest", "EmbedRequest"]


# --- Worker 退出时关闭网关客户端 ---


@pytest.mark.temporal
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("with_address", [True, False], ids=["grpc-gateway", "stub-model"])
async def test_worker_closes_its_gateway_clients_when_it_exits(
    temporal_env: WorkflowEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_address: bool
) -> None:
    """交付 3：Worker 退出（serve 被取消；Ctrl+C 时 asyncio.run 就是这样收尾）时，关闭它创建的每个 GrpcModelGateway
    （Node Executor、Planner、Reviewer 共用一个还是各建一个都行，建了几个就关几个）。没设地址时是桩模型，退出照常、不报错。"""
    created: list[GrpcModelGateway] = []
    closed: list[GrpcModelGateway] = []
    original_init = GrpcModelGateway.__init__

    def recording_init(self: GrpcModelGateway, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        created.append(self)

    async def recording_aclose(self: GrpcModelGateway) -> None:
        closed.append(self)

    monkeypatch.setattr(GrpcModelGateway, "__init__", recording_init)
    monkeypatch.setattr(GrpcModelGateway, "aclose", recording_aclose)

    # serve 组装好 Worker 之后再取消它，模拟运行中退出。
    built = asyncio.Event()
    original_build = worker_module.build_workers

    def recording_build(*args: Any, **kwargs: Any) -> Any:
        workers = original_build(*args, **kwargs)
        built.set()
        return workers

    monkeypatch.setattr(worker_module, "build_workers", recording_build)

    async with serve(FakeModelGateway()) as address:
        config = WorkerConfig(
            temporal_address=temporal_env.client.service_client.config.target_host,
            namespace=temporal_env.client.namespace,
            data_dir=tmp_path / "data",
            planner="model",
            reviewer="model",
            model_gateway_address=address if with_address else "",
        )
        task = asyncio.create_task(worker_module.serve(config, [names.DECISION_QUEUE, names.MODEL_QUEUE]))
        try:
            await asyncio.wait_for(built.wait(), timeout=30)
            await asyncio.sleep(0.5)
        finally:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task  # 收尾时抛别的异常（例如对桩模型调用 aclose）这里会暴露出来

    if not with_address:
        assert created == [], "没设 AR_MODEL_GATEWAY_ADDRESS 时不建 GrpcModelGateway"
        return
    assert created, "设了 AR_MODEL_GATEWAY_ADDRESS 时 Worker 应建 GrpcModelGateway"
    unclosed = [g for g in created if not any(g is c for c in closed)]
    assert not unclosed, f"Worker 退出后还有 {len(unclosed)}/{len(created)} 个 GrpcModelGateway 没关"
