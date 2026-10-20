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
class GateRecord:
    """write_irreversible 的 Node 执行前通过的人工审批（附录 D.4、F.3）。"""

    gate_node: str
    option: str
    approved_by: str = ""


@dataclass
class NodeInput:
    tenant: Tenant
    task_id: str
    round_seq: int
    # 一个 Atomic Node（atomic_node.schema.json）。inputs 里的 $ 引用已由 Stage Runner 解析成上游的 result。
    node: dict[str, Any]
    # 通过的审批；只有 write_irreversible 的 Node 有。
    gate: GateRecord | None = None
    # before_node Hook 的 inject 结果（hook_result.schema.json 的 injection），由 Context Builder 放进对应分区。
    injections: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StageResult:
    """交给 Reviewer 的一个 Node 的结果：只有 result，不带 rationale（只写入 trace）与规划过程。

    status 取 node_status：succeeded、failed、skipped、cancelled。failure_class 取附录 D.6 的分类，
    未分类的失败为空。quality 是质量包装的结局，例如 {"type": "revise", "rounds": 2, "passed": false}。
    """

    role: str
    status: str
    result: Any = None
    failure_class: str = ""
    error: str = ""
    quality: dict[str, Any] | None = None


@dataclass
class StagePolicy:
    """Stage 执行的上限（计划 S-04 难点“扇出上限”），来自租户策略。"""

    # 一个 Task 工作流里同时执行的叶子 Node（activity、子工作流）个数。
    max_parallel: int = 8
    # 一个工作流里内联展开的 Node 个数上限；更大的批量拆到子工作流。
    max_inline_nodes: int = 200
    # 批量展开时每个子工作流处理的条目数，与同时运行的子工作流个数。
    batch_size: int = 50
    batch_parallel: int = 4
    # write_irreversible 前人工审批的等待上限；超时按拒绝处理（附录 D.3 human：超时按模板默认处理）。
    approval_timeout_s: int = 24 * 3600
    # human 节点没写 timeout_s 时的等待上限。
    human_timeout_s: int = 24 * 3600


@dataclass
class StagePolicyRequest:
    tenant: Tenant
    task_id: str
    pinned: dict[str, Any]


@dataclass
class TemplateInfo:
    """Node Catalog 里一个 Node Template 的执行方式（附录 D.7）。"""

    executor: str
    effect: str
    # model 节点的输出类别：generate、score、embed、classify
    output: str = ""


@dataclass
class CatalogRequest:
    tenant: Tenant
    task_id: str
    templates: list[str]
    tools: list[str]
    pinned: dict[str, Any] = field(default_factory=dict)


@dataclass
class CatalogInfo:
    templates: dict[str, TemplateInfo] = field(default_factory=dict)
    # 工具名 → effect
    tools: dict[str, str] = field(default_factory=dict)


@dataclass
class TemplateExpansionRequest:
    tenant: Tenant
    task_id: str
    round_seq: int
    # template 模式的 Goal Node（goal_node.schema.json）
    node: dict[str, Any]
    pinned: dict[str, Any] = field(default_factory=dict)


@dataclass
class TemplateExpansion:
    """Recipe 展开的子图（附录 E.1 template）。两种形状二选一：

    - nodes + output：固定子图，Node 用局部 id；output 是给出 Goal 结果的那个局部 id。
    - per_item + items + collect：对每个条目套用同一条链，collect 是链上给出条目结果的局部 id；
      Goal 的结果是 {"items": [...]}，按条目顺序。大批量时按条目拆到子工作流。
    """

    nodes: list[dict[str, Any]] = field(default_factory=list)
    output: str = ""
    per_item: list[dict[str, Any]] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)
    collect: str = ""


@dataclass
class ToolLoopStepInput:
    """tool_loop 的一步“思考”：看目标与此前的动作和观察，决定调哪个工具或结束（附录 E.1）。"""

    tenant: Tenant
    task_id: str
    round_seq: int
    # Goal Node（goal_node.schema.json），inputs 已解析
    node: dict[str, Any]
    iteration: int
    # 此前每一步：{"tool", "args", "observation"} 或 {"tool", "args", "error"}；只有结果，没有 rationale。
    history: list[dict[str, Any]]
    # 这一步思考的 Node id：<Goal id>.<2i-1>
    step_id: str = ""
    # sub_agent 的独立上下文
    isolated: bool = False


@dataclass
class ToolLoopStep:
    # tool：调用工具；finish：结束并给出结果
    action: str
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None


@dataclass
class ToolCallInput:
    tenant: Tenant
    task_id: str
    round_seq: int
    node_id: str
    tool: str
    args: dict[str, Any]
    effect: str


@dataclass
class HumanQuestion:
    """经 HumanChannel 发出的问题（设计书 4.11）。回复以 human_reply 信号送回 Task 工作流，Node Executor 不占线程等待。"""

    tenant: Tenant
    task_id: str
    # human 节点为 Node id；write_irreversible 前的审批 gate 为 <id>.1
    question_id: str
    text: str
    options: list[str]
    # ISO 8601，带时区；由工作流按 workflow.now() + 超时算出
    deadline: str
    state_summary: str = ""
    # 同一 Node id 在不同 Round 里会再问一次；HumanChannel 里的问题 id 带上 Task 与 Round，见 channel_question_id。
    round_seq: int = 1


@dataclass
class HumanCancel:
    tenant: Tenant
    task_id: str
    question_id: str
    round_seq: int = 1


@dataclass
class ReconcileResult:
    """写操作结果未知时按幂等键对账（附录 K.4）：found 表示外部已经生效，output 是当时的 Node Output。"""

    found: bool
    output: dict[str, Any] | None = None
    reason: str = ""


@dataclass
class HookCall:
    """在一个挂载点执行一组 Hook（附录 F.1）。subject 是这次动作的对象：Stage、Node 的输入或输出。"""

    tenant: Tenant
    task_id: str
    round_seq: int
    mount: str
    hooks: list[str]
    subject: dict[str, Any]


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
