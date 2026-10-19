"""activity 的输入输出。只在 Python 内部使用，按 Temporal 的 JSON 编码传输。

Stage、Verdict、Node Output、Task State 是 /schemas 定义的 JSON 载荷，这里用 dict 携带（设计书 4.1）。
每个 activity 的输入都带租户上下文（CLAUDE.md“每次 Provider 调用都带租户上下文”）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    user_id: str = ""
    workspace_id: str = ""
    session_id: str = ""


@dataclass
class PlanRoundInput:
    tenant: Tenant
    task_id: str
    round_seq: int
    task_state: dict[str, Any]
    message: str
    inputs: dict[str, Any]
    attachments: list[str]
    # Task 启动时固定的版本：recipe、node_templates、reasoning_templates、model_profiles、tenant_policy。
    pinned: dict[str, Any]


@dataclass
class PlanRoundOutput:
    # stage.schema.json
    stage: dict[str, Any]
    # Triage 级别：fast、light、full、replan
    triage_level: str


@dataclass
class NodeInput:
    tenant: Tenant
    task_id: str
    round_seq: int
    # Stage 里的一个 Atomic Node（atomic_node.schema.json）
    node: dict[str, Any]


@dataclass
class StageResult:
    """交给 Reviewer 的一个 Node 的结果：只有 result，不带 rationale（只写入 trace）与规划过程。"""

    role: str
    status: str
    result: Any = None


@dataclass
class ReviewRoundInput:
    """Reviewer 的输入只有 Stage 结果、Task State 与验收条件（设计书 3.2.1 第 4 条）。"""

    tenant: Tenant
    task_id: str
    round_seq: int
    task_state: dict[str, Any]
    stage_results: dict[str, StageResult]
    done_when: str


@dataclass
class FinalizeTaskInput:
    """收尾写回（附录 B.9）。"""

    tenant: Tenant
    task_id: str
    # task_status 的取值：done、aborted、cancelled、failed
    status: str
    task_state: dict[str, Any]
    verdict: dict[str, Any] | None = None
    deliver_node: str = ""
    artifacts: list[str] = field(default_factory=list)
