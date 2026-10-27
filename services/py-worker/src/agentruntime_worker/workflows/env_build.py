"""按项目定制的运行环境的构建工作流（D-17；做什么见 agentruntime_worker.sandbox.environments）。

每个（租户，环境定义的键）一个工作流，ID 是 env-build-<租户>-<键的前 32 位>：同一个定义同时只有一个构建。工作流只排一个
build_environment activity（program 队列：起构建的 Job、等它、把产物推进仓库、记下结果）；构建可能要半小时，Worker 中途
重启时 activity 重试一次。工作流代码只排 activity，是确定的。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agentruntime_worker import names


@dataclass(frozen=True)
class EnvBuildInput:
    tenant_id: str
    key: str
    timeout_s: int = 1800
    activity_queue: str = names.PROGRAM_QUEUE


def build_workflow_id(tenant_id: str, key: str) -> str:
    return f"env-build-{tenant_id}-{key[:32]}"


@workflow.defn(name=names.ENV_BUILD_WORKFLOW)
class EnvironmentBuildWorkflow:
    @workflow.run
    async def run(self, inp: EnvBuildInput) -> str:
        return await workflow.execute_activity(
            names.BUILD_ENVIRONMENT,
            inp,
            result_type=str,
            task_queue=inp.activity_queue,
            start_to_close_timeout=timedelta(seconds=inp.timeout_s + 900),
            retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=30)),
        )


__all__ = ["EnvBuildInput", "EnvironmentBuildWorkflow", "build_workflow_id"]
