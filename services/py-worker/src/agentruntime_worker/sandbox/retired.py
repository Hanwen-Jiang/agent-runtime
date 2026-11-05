"""搬进 Sandbox Service 之后留下的 activity（SBX-01）：对账与按项目构建环境都在服务里做了。

工作流的定义（workflows/sandbox_reconcile.py、workflows/env_build.py）不改：Temporal 里还可能有它们的运行与历史，回放要能过。
它们排的 activity 换成这里的：对账什么都不做（服务自己按间隔对账），构建报不可重试的错误（服务自己起构建，构建状态在环境表的
sandbox_images 里，卡在 building 的由服务按时限重来）。Worker 启动时还会结束还在跑的对账工作流（见 worker.retire_sandbox_reconcile）。
"""

from __future__ import annotations

import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentruntime_worker import names
from agentruntime_worker.workflows.env_build import EnvBuildInput
from agentruntime_worker.workflows.sandbox_reconcile import ReconcileInput, ReconcileReport

log = logging.getLogger(__name__)


class RetiredSandboxActivities:
    @activity.defn(name=names.RECONCILE_SANDBOXES)
    async def reconcile_sandboxes(self, inp: ReconcileInput) -> ReconcileReport:
        del inp
        return ReconcileReport(skipped_backends=["对账已经搬进 Sandbox Service（SBX-01）"])

    @activity.defn(name=names.BUILD_ENVIRONMENT)
    async def build_environment(self, inp: EnvBuildInput) -> str:
        log.info("项目环境的构建已经搬进 Sandbox Service（SBX-01），这个旧的构建工作流不再构建：租户 %s，键 %s", inp.tenant_id, inp.key[:12])
        raise ApplicationError("项目环境的构建已经搬进 Sandbox Service（SBX-01）", type="moved_to_sandbox_service", non_retryable=True)

    def activities(self) -> dict[str, object]:
        return {names.RECONCILE_SANDBOXES: self.reconcile_sandboxes, names.BUILD_ENVIRONMENT: self.build_environment}


__all__ = ["RetiredSandboxActivities"]
