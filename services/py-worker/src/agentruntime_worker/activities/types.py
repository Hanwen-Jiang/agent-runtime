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
    # 之前的轮次产出的 Task Plan（task_plan.schema.json）；首轮为空（S-09）。
    task_plan: dict[str, Any] | None = None
    # 上一轮：{"stage", "results": {node_id: StageResult 的字段}, "verdict"}；首轮为空。replan 时
    # verdict.replan.keep_nodes 是要保留的 Node（S-09）。
    last_round: dict[str, Any] | None = None
    # 决策调用的重试用完或到了总时限（TMP 计划第 4 节）：不调模型，直接给保守规划。
    no_model: bool = False


@dataclass
class PlanRoundOutput:
    # stage.schema.json
    stage: dict[str, Any]
    # Triage 级别：fast、light、full、replan
    triage_level: str
    # Task Plan（task_plan.schema.json）；fast 没有，后续轮次没变时可以为空（S-09）。
    task_plan: dict[str, Any] | None = None
    # 这次规划的模型花费（分诊与规划），计入 Round 的成本（成本口径的人定方案，2026-10-24）。
    cost_usd: float = 0.0


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
    # 直接执行这个 Node 花的钱（Node Executor 的 meta.cost_usd，S-10）。质量包装与展开出的子 Node 的成本
    # 只计入 Round 的成本（Stage Runner 的 spent_usd）。
    cost_usd: float = 0.0
    # 实际服务这个结果的模型供应商（model 节点的 meta.vendor、tool_loop 最后一步的）；program 节点为空。
    # Reviewer 调用时避开它（附录 H.3；R-02 评审第 4 节第 1 条）。
    vendor: str = ""
    # 失败说明的代码与参数（TMP 计划第 5 节；例如 budget_exhausted 与 {budget, limit, used}），进度事件 node_finished 带出。
    error_code: str = ""
    params: dict[str, Any] | None = None
    # 执行中发现的硬信号（附录 B.6；TMP 计划第 2 节的 environment：列工作区根得到空目录），交给 Reviewer。
    signals: list[str] = field(default_factory=list)
    # 执行这个 Node 发出的工具调用（tool_loop 的调用，含 sub_agent 子工作流里的）；只用来把子工作流里的次数带回父 Stage。
    tool_calls: int = 0


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
    # 租户策略与 Task 固定的 Recipe 声明的 Hook：挂载点 → Hook 名（T-09 骨架）。
    hooks: dict[str, list[str]] = field(default_factory=dict)


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
    # 来自 Registry（T-09 骨架）：Stage Runner 据此核对 Atomic Node 的 executor、effect；桩 Catalog 为 False。
    authoritative: bool = False


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
    # Context Builder 用：Task State 的精确状态单独成段注入，Task Plan 的 context 给出工作模式与阈值。
    # sub_agent（isolated）不带。只是输入，不改变命令序列。
    task_state: dict[str, Any] = field(default_factory=dict)
    task_plan: dict[str, Any] | None = None


@dataclass
class ToolLoopStep:
    # tool：调用工具；finish：结束并给出结果
    action: str
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    # 这一步的模型花费（含 Context Builder 的摘要与 Probe），计入 Round 的成本；服务这一步的供应商。
    cost_usd: float = 0.0
    vendor: str = ""


@dataclass
class ToolCallInput:
    tenant: Tenant
    task_id: str
    round_seq: int
    # 这次调用的 Node id：<Goal id>.<2i>
    node_id: str
    tool: str
    args: dict[str, Any]
    effect: str
    # 通过的人工审批；只有 effect 为 write_irreversible 的调用有，审批 id 是 <node_id>.1（D16）。
    gate: GateRecord | None = None


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
    """Reviewer 的输入只有 Stage 结果、Task State 与验收条件（设计书 3.2.1 第 4 条），不含 Planner 的规划过程。

    S-10 另带本轮执行的 Stage（判 continue 要看 exit_when，诊断形状、选修复范围要看 Node 结构）与之前各 Round 的摘要
    （history：Verdict、升级的步骤与失败的 Node，由工作流从 Task State 与之前的结果得出）。S-10 之前的历史里没有这两项。
    """

    tenant: Tenant
    task_id: str
    round_seq: int
    task_state: dict[str, Any]
    stage_results: dict[str, StageResult]
    done_when: str
    stage: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    # 决策调用的重试用完或到了总时限（TMP 计划第 4 节）：不调模型，只按规则判定。
    rules_only: bool = False


@dataclass
class ReviewRoundOutput:
    """Reviewer 的输出（S-10）：Verdict（verdict.schema.json），以及要写进 Task State 的汇总
    （facts、assumptions_verified、assumptions_broken、open_questions、artifacts）。S-03 的桩只返回 Verdict 本身。"""

    verdict: dict[str, Any]
    state_update: dict[str, Any] = field(default_factory=dict)
    # 这次评审的模型花费，计入 Round 的成本（成本口径的人定方案，2026-10-24）。
    cost_usd: float = 0.0


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
    # 各 Round 的 Stage 摘要：{"seq", "goal", "nodes"（顶层 Node 数）, "tiers", "shape"}，写回 Recipe 的效果用（S-10）；
    # shape 是 Stage 的形状，记进 runtime.round span（T-15）。
    stages: list[dict[str, Any]] = field(default_factory=list)
    # 任务族（Task Plan 的 triage.task_family；没有时为空），记进 invoke_workflow span（T-15）。
    task_family: str = ""
