"""沙箱环境表的对账工作流（D-11；做什么见 agentruntime_worker.sandbox.reconcile）。

平台级、固定的工作流 ID（names.SANDBOX_RECONCILE_WORKFLOW_ID），Worker 启动时确保它在跑（已在跑就不再起）。每隔 interval_s
在 program 队列上跑一次 reconcile_sandboxes activity，跑满 rounds 次后 continue_as_new。工作流代码只排 activity 与计时器，
是确定的；调后端、读写环境表都在 activity 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agentruntime_worker import names


@dataclass(frozen=True)
class ReconcileInput:
    interval_s: int = 60
    rounds: int = 120
    grace_s: int = 600
    # activity 所在的队列（测试用别的队列，免得与同一个开发服务上的别的 Worker 抢）。
    activity_queue: str = names.PROGRAM_QUEUE


@dataclass
class ReconcileReport:
    listed: int = 0
    orphans_destroyed: int = 0
    lost: int = 0
    reaped: int = 0
    destroy_retried: int = 0
    stale_failed: int = 0
    # 空闲到期、但它服务的 Task 还没结束（多半在等人），顺延保留的（docs/plan/feedback-2026-11-01.md N-05）。
    held: int = 0
    skipped_backends: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # 挂卷的工作区所在卷的已用比例（DISK-A）；没有挂卷的工作区根时为 None。
    volume_used_ratio: float | None = None
    # 这一轮转冷了几个挂卷的工作区（DISK-B）。
    cooled: int = 0


@workflow.defn(name=names.SANDBOX_RECONCILE_WORKFLOW)
class SandboxReconcileWorkflow:
    @workflow.run
    async def run(self, inp: ReconcileInput) -> None:
        for _ in range(max(1, inp.rounds)):
            await workflow.execute_activity(
                names.RECONCILE_SANDBOXES,
                inp,
                result_type=ReconcileReport,
                task_queue=inp.activity_queue,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5)),
            )
            await workflow.sleep(timedelta(seconds=max(1, inp.interval_s)))
        workflow.continue_as_new(inp)


__all__ = ["ReconcileInput", "ReconcileReport", "SandboxReconcileWorkflow"]
