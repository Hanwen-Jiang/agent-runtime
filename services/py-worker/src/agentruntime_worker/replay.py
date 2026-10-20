"""重放测试工具（计划 S-03 交付）：用当前代码回放 Task 工作流（及 SubAgentWorkflow、SubgraphWorkflow）的事件历史。

    python -m agentruntime_worker.replay --dir <事件历史目录>

目录里每个 *.json 是一份事件历史（`temporal workflow show --output json` 或 SDK 的 WorkflowHistory.to_json()）。
全部回放成功返回 0，有失败返回 1，目录里没有历史或用法错误返回 2。

带 Claim-Check 引用的历史（S-06）从 ObjectStore 取回对象：位置与 Worker 相同（AR_OBJECT_STORE_DIR、AR_SECRETS_FILE，
默认在 AR_DATA_DIR 下），租户取自每份历史第一个事件的 memo。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter
from temporalio.worker import Replayer, WorkflowReplayResult

from agentruntime_core.adapters.claim_check import ClaimCheck, StaticTenant, TenantResolver, tenant_from_history


def load_histories(directory: Path) -> list[WorkflowHistory]:
    histories = []
    for path in sorted(Path(directory).glob("*.json")):
        workflow_id = path.stem.split("__")[0] or path.stem
        histories.append(WorkflowHistory.from_json(workflow_id, path.read_text("utf-8")))
    return histories


class _NoTenant:
    async def tenant_for(self, context: object) -> None:
        return None


def _converter(claim_check: ClaimCheck | None, history: WorkflowHistory) -> DataConverter:
    if claim_check is None:
        return DataConverter.default
    tenant = tenant_from_history(history)
    resolver: TenantResolver = StaticTenant(tenant) if tenant is not None else _NoTenant()
    return dataclasses.replace(DataConverter.default, payload_codec=claim_check.codec(resolver))


async def replay_histories(
    histories: Sequence[WorkflowHistory], workflows: Sequence[type] | None = None, *, claim_check: ClaimCheck | None = None
) -> list[WorkflowReplayResult]:
    """逐份回放，不在第一份失败时停下。workflows 默认是 Task 工作流与它的两种 Python 子工作流。
    claim_check 给出时，按每份历史 memo 里的租户取回 Claim-Check 卸载的载荷。"""
    if workflows is None:
        from agentruntime_worker.workflows.stage_runner import SubAgentWorkflow, SubgraphWorkflow
        from agentruntime_worker.workflows.task import TaskWorkflow

        workflows = [TaskWorkflow, SubAgentWorkflow, SubgraphWorkflow]
    results = []
    for history in histories:
        replayer = Replayer(workflows=list(workflows), data_converter=_converter(claim_check, history))
        results.append(await replayer.replay_workflow(history, raise_on_replay_failure=False))
    return results


def claim_check_from_env() -> ClaimCheck:
    from agentruntime_worker.config import WorkerConfig
    from agentruntime_worker.executor.dev import data_root, dev_object_store, dev_secret_store

    config = WorkerConfig.from_env(os.environ)
    root = data_root(config)
    return ClaimCheck(dev_object_store(config, root), dev_secret_store(config, root), threshold=config.claim_check_threshold)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentruntime_worker.replay", description="回放 Task 工作流的事件历史")
    parser.add_argument("--dir", required=True, type=Path, help="事件历史目录")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 2)
    histories = load_histories(args.dir)
    if not histories:
        print(f"{args.dir} 里没有事件历史", file=sys.stderr)
        return 2
    results = asyncio.run(replay_histories(histories, claim_check=claim_check_from_env()))
    failed = [(h, r) for h, r in zip(histories, results, strict=True) if r.replay_failure is not None]
    for history, result in failed:
        print(f"回放失败 {history.workflow_id}：{result.replay_failure!r}", file=sys.stderr)
    print(f"回放 {len(histories)} 份，失败 {len(failed)} 份")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
