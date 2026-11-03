"""工作流的版本分支（workflow.patched 的 id）一览，以及新的运行在开头一次问完（S-04 的缺陷修复，见 TMP 计划 T-14 之后的记录）。

回放时 patched() 只在回放到记有这个标记的工作流任务时才返回 True。local activity（Hook 的 run_hooks）的结果记在 marker 里：
它正在跑时来了查询（例如客户端轮询 QueryTask），SDK 会只带着这个 marker 先提交工作流任务，local activity 之后的代码落进
下一个工作流任务；回放时 SDK 却在上一个工作流任务里就把结果交给工作流，紧接着第一次问的版本分支看不到下一个工作流任务里的
标记，返回 False，走了旧路径（temporalio 1.34.0；sdk-rust#1616 没有覆盖这种情况）。之后紧跟的若是普通命令，回放不受影响。

所以新的运行在第一个工作流任务里、任何 local activity 之前，就按 AT_START 的顺序把全部版本分支问一遍：patched() 每次运行
只在第一次问时记标记，之后在原位置的调用直接拿到同一个值、不再发命令。这一步本身也是版本分支（PATCHES_AT_START）：之前的
历史没有这个标记，各版本分支仍在原位置第一次问，按原来的命令序列回放（已经录下切分的旧历史修不了）。

AT_START 只能在末尾追加：追加的版本分支，对已经开始的运行在开头回放时返回 False（并记住），原位置也就一直走旧路径；
改顺序或删掉会改变新历史开头的命令序列。工作流代码里每个 patched() 的 id 都要定义在这里并列进 AT_START
（tests/test_patches_at_start.py 核对）。

是工作流代码：只调用 temporalio.workflow。
"""

from __future__ import annotations

from temporalio import workflow

# Stage Runner（S-04）改变了命令序列：新的运行走这个版本分支，S-03 留下的历史仍按旧路径回放。
STAGE_RUNNER_PATCH = "s04-stage-runner"
# S-05 改变命令序列的两处各有版本分支：human 节点与审批先经 HumanChannel 发问并设超时；写操作 activity 超时后按幂等键对账。
HUMAN_CHANNEL_PATCH = "s05-human-channel"
RECONCILE_PATCH = "s05-reconcile"
# D16：tool_loop 里每次调用 write_irreversible 的工具前都插入人工审批（设计书 3.2.3、附录 E.1）。
TOOL_LOOP_APPROVAL_PATCH = "d16-tool-loop-approval"
# 发问之前先登记要等的回复，问题刚发出就到的回复不再被当作“不匹配”丢掉。
HUMAN_REPLY_RACE_PATCH = "d16-expect-reply-before-ask"
# S-10：按 Verdict 的去向走（repair 重跑、问人、rollback、reroute……）与收尾先通知再写回，改变了命令序列。
REVIEWER_PATCH = "s10-reviewer"
# T-09：Policy Engine（策略声明的 Hook、Task 级挂载点、before_finish 的驳回上限、按 Registry 核对 effect）改变了命令序列。
POLICY_PATCH = "t09-policy-engine"
# T-11b 加入了推送进度事件的 activity，改变了命令序列：新的运行走这个版本分支，之前的历史照旧回放。
EVENTS_PATCH = "t11b-progress-events"

# R-01 评审之后（2026-10-24）：没成功的 Node 也执行 audit_log（多一次 local activity）；tool_loop 每一步之前查 Task 的预算，
# 用完就停（成本口径的人定方案）。都改变了命令序列。
R01_FIXES_PATCH = "r01-audit-and-budget"

# 2026-10-28（人审通过的 S-01 改动）：顶层 Node 开始时推 node_started，tool_loop 里每次工具调用前后推 tool_status。
# 多了推送进度事件的 activity，改变了命令序列。
PROGRESS_DETAIL_PATCH = "progress-node-and-tool"

# TMP 计划（eval-observability-and-runtime-fixes，2026-10-28）第 3 节：时间与工具调用预算（Round 开头查、用完问人，
# tool_loop 里用完就停）。多了问人与推送的命令，改变了命令序列。
BUDGETS_PATCH = "tmp-time-tool-budgets"
# 同一计划第 4 节：plan_round、review_round、tool_loop_step 由工作流重试，重试之前推 decision_retry；用完退回不调模型的做法。
DECISION_RETRY_PATCH = "tmp-decision-retry"
# 同一计划第 5 节：问人后停止或没有回复时另推一个带代码的 abort verdict 事件，多了推送的命令。
CODES_PATCH = "tmp-message-codes"
# docs/plan/running-task-interaction.md 第 1 节：一次模型回答里的多个工具调用都执行，多了并发的 activity，改变了命令序列。
PARALLEL_TOOL_CALLS_PATCH = "parallel-tool-calls"
# 同一计划第 3 节：每次模型调用之后推 context_usage，多了推送的命令。
CONTEXT_USAGE_PATCH = "context-usage-events"
# 同一计划第 2 节：运行中的纠偏与立刻打断（工具调用被拦下、Stage 被打断、回 Planner，多了推送 guidance_updated 的命令）。
GUIDANCE_PATCH = "guidance-and-interrupt"
# 发问的 activity 还没结束时被立刻打断：问题可能已经发出去了，也要撤回（多一次 cancel_human）。
INTERRUPT_WITHDRAW_PATCH = "interrupt-withdraws-question"

# docs/plan/feedback-2026-11-01.md N-01：嵌在文字里的引用、引用之前各轮的结果、deliver 没写引用时带上上游。
# 原来校验不过的 Stage 现在能跑，原来原样传下去的占位符现在可能让 Node 失败，改变了命令序列。
REFERENCES_PATCH = "n01-references"

# Q87–Q93：Activity 的 ContextBuilder 版本在 Task 开始时固定；旧 Task 与它后续启动的子工作流保留 v1。
CONTEXT_BUILDER_V2_PATCH = "q87-context-builder-v2"

# docs/plan/feedback-2026-11-01.md N-04：Task Plan 的整体步骤写进 Task State，变了就推 plan_updated（规划之后、收尾之前），多了推送的命令。
PLAN_PATCH = "n04-task-plan"

# 新的运行在第一个工作流任务里一次问完全部版本分支（模块说明）。
PATCHES_AT_START = "s04-patches-at-start"
# 只能在末尾追加。
AT_START = (
    REVIEWER_PATCH,
    POLICY_PATCH,
    EVENTS_PATCH,
    STAGE_RUNNER_PATCH,
    HUMAN_CHANNEL_PATCH,
    HUMAN_REPLY_RACE_PATCH,
    RECONCILE_PATCH,
    TOOL_LOOP_APPROVAL_PATCH,
    R01_FIXES_PATCH,
    PROGRESS_DETAIL_PATCH,
    BUDGETS_PATCH,
    DECISION_RETRY_PATCH,
    CODES_PATCH,
    PARALLEL_TOOL_CALLS_PATCH,
    CONTEXT_USAGE_PATCH,
    GUIDANCE_PATCH,
    INTERRUPT_WITHDRAW_PATCH,
    REFERENCES_PATCH,
    CONTEXT_BUILDER_V2_PATCH,
    PLAN_PATCH,
)


def at_start() -> bool:
    """在工作流 run 的第一句调用（第一个工作流任务里、任何 local activity 之前）。

    新的运行：记下 PATCHES_AT_START，再按 AT_START 的顺序问一遍全部版本分支，返回 True。
    之前的历史没有 PATCHES_AT_START：什么也不问，返回 False，各版本分支仍在原位置第一次问。
    """
    if not workflow.patched(PATCHES_AT_START):
        return False
    for patch_id in AT_START:
        workflow.patched(patch_id)
    return True
