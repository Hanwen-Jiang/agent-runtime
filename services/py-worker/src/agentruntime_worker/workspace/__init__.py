"""Workspace 层的 Worker 一侧（设计书改动稿 W2；D-06，docs/plan/workspace-layer.md）。

- archive：工作区的存档与恢复（内容寻址的块、清单、快照表），存档模式的沙箱后端经 SandboxBroker 用它。
- operations：Workspace API 的操作（建与克隆、diff、提交、推送、存档、删除），在承载工作区的沙箱里执行；
  task-service 的 WorkspaceService 经 Temporal 的 WorkspaceOperationWorkflow 调它。
- timing：Workspace 操作与 shell_run 的分段耗时、进出 Worker 的字节数（PERF-00、WK-LITE-03）。
"""

from agentruntime_worker.workspace.archive import MIGRATIONS, Snapshot, WorkspaceArchiver, WorkspaceRecord, blob_key, manifest_key

__all__ = ["MIGRATIONS", "Snapshot", "WorkspaceArchiver", "WorkspaceRecord", "blob_key", "manifest_key"]
