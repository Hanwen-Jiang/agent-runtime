"""重放测试工具（计划 S-03 交付）：用当前代码回放 Task 工作流（及 SubAgentWorkflow、SubgraphWorkflow）的事件历史。

    python -m agentruntime_worker.replay --dir <事件历史目录>

目录里每个 *.json 是一份事件历史（`temporal workflow show --output json` 或 SDK 的 WorkflowHistory.to_json()）。
全部回放成功返回 0，有失败返回 1，目录里没有历史或用法错误返回 2。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer, WorkflowReplayResult


def load_histories(directory: Path) -> list[WorkflowHistory]:
    histories = []
    for path in sorted(Path(directory).glob("*.json")):
        workflow_id = path.stem.split("__")[0] or path.stem
        histories.append(WorkflowHistory.from_json(workflow_id, path.read_text("utf-8")))
    return histories


async def replay_histories(histories: Sequence[WorkflowHistory], workflows: Sequence[type] | None = None) -> list[WorkflowReplayResult]:
    """逐份回放，不在第一份失败时停下。workflows 默认是 Task 工作流与它的两种 Python 子工作流。"""
    if workflows is None:
        from agentruntime_worker.workflows.stage_runner import SubAgentWorkflow, SubgraphWorkflow
        from agentruntime_worker.workflows.task import TaskWorkflow

        workflows = [TaskWorkflow, SubAgentWorkflow, SubgraphWorkflow]
    replayer = Replayer(workflows=list(workflows))
    return [await replayer.replay_workflow(h, raise_on_replay_failure=False) for h in histories]


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
    results = asyncio.run(replay_histories(histories))
    failed = [(h, r) for h, r in zip(histories, results, strict=True) if r.replay_failure is not None]
    for history, result in failed:
        print(f"回放失败 {history.workflow_id}：{result.replay_failure!r}", file=sys.stderr)
    print(f"回放 {len(histories)} 份，失败 {len(failed)} 份")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
