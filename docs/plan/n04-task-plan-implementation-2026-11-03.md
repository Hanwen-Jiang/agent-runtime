# N-04 Task Plan 实施记录（2026-11-03，E 线）

对应 [feedback-2026-11-01.md](feedback-2026-11-01.md) N-04、人工验收基线 Q55（Plan 是持久状态）、Q56（plan_updated 是可重放事件），
契约清单 `tests/contract/fixtures/task_plan_contract_cases.json`。此文只记实现增量，不改冻结的设计书。

## 契约（CONTRACT DRAFT 提交；2026-11-03 人审通过，已合进 main）

- `defs.schema.json`：`plan_step_status`（pending、active、done、blocked）、`plan_step`（id、title、status、round）、`plan_steps`
  （3～8 步，`maxContains: 1` 限制最多一步 active）；`progress_event` 加 `plan_updated`。
- `task_plan.schema.json` 加可选的 `steps`；`task_state.schema.json` 加可选的 `plan`（steps、round_seq、current_step）；
  `task_event_payload.schema.json` 加 `plan_updated`（完整快照，不是增量）。
- `taskapi.v1.TaskEventType` 加 `TASK_EVENT_TYPE_PLAN_UPDATED = 17`。只新增，`buf breaking` 对未改的基线通过；生成代码一并提交。
- schema 表达不了的两条（id 不重复、round 不晚于快照的 Round）在 `workflows/plan.py` 检查，Planner 与 Task 工作流共用同一个函数。

## 实现

- **唯一写者**：Task 工作流（版本分支 `n04-task-plan`，追加在 `AT_START` 末尾）。Planner 每轮给出的 steps 通过检查、又与现有的不同时，
  替换 Task State 的 `plan`，在 `stage_started` 之前推 `plan_updated`；没给或不合格（fast、light、保守规划、原地重跑、模型写错）
  时保留现有的一份。rollback 的快照不含 `plan`（`SNAPSHOT_KEYS` 不变），进度不随事实回滚。
- **收尾**：通知 Session 之前，正在做的一步按结局记为 done（done）或 blocked（abort、失败），推最后一份（去重键
  `r<Round>/plan_updated/final`），所以 `task_finished` 仍是最后一个事件。取消的 Task 只改 Task State 不推（取消时推送会被一并取消）。
- **Planner**：规则 10 要求第一次完整规划（Task State 里还没有 plan）给出 3～8 步，之后照 Task State 的 plan 更新状态；没给或
  不合格算一次不合格，带说明重问一次，再不行走保守规划（保守规划不带步骤，之前的计划保留）。protobuf Struct 把整数变成 float，
  round 认整数值的 float 并规整成 int。
- **Context Builder**：brief 区加“整体计划”一行一步（`plan.brief_lines`），遮蔽、摘要、reset 都不动 brief；同一 Round 内每一步
  相同，不破坏提示缓存（断点 3 只在 Round 之间变）。
- **Task API / SSE**：`QueryTask` 的 `task_state.plan` 与 SSE 的 `plan_updated` 内容相同；N-19 的会话快照另外把每个 Task 最新的
  一份带上（见 N-19/N-20 记录）。

## 决策表

| 项目 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 计划在 Task State | 计划只在每轮的 Planner 输出里，后面的轮看不到整体 | 2026-11-01 日志：只有每轮的 “Plan: …” | Task State 加 `plan`，工作流写 | 一个状态源，查询、Planner、Context Builder 读同一份 | Task State 多 3～8 行 | 低 | 无新表 | Planner 写坏的步骤被丢弃，界面停在旧计划 | N-04 | KEEP |
| 完整快照事件 | 增量事件在重连后拼不完整 | 基线 Q56、清单 event_full_snapshot | 每次变化推完整快照，未变不推 | 客户端丢旧重建，幂等 | 每轮至多一个小事件 | 低 | 无 | 同 Round 重放靠去重键 | N-04 | KEEP |
| 步骤状态由谁推进 | 工作流猜进度会与模型的意图不一致 | 设计书 3.2.2：Planner 首轮出 Task Plan，每轮出 Stage | 状态由 Planner 写，工作流只校验；收尾时规则化地关掉正在做的一步 | 不在工作流里猜语义 | 无 | 低 | 无 | Planner 不更新时计划显得停滞 | N-04 | KEEP |
| 第一次完整规划必须带步骤 | 回退规划多了 | 规则 10；评测的回退率门槛 5% | 只在没有计划时要求；不合格重问一次 | 计划一开始就有 | 偶尔多一次规划调用 | 低 | 需看评测的回退率 | 回退率上升 | N-04 | KEEP，真模型评测时复核 |
| 计划版本号/哈希 | — | 基线 DEFER | 复用 event_id 与 round_seq | — | — | — | — | — | — | DEFER（不做） |

## 验证

- `tests/test_n04_task_plan.py`：20 条，覆盖清单的 10 条（3～8 步边界、两步 active、round 晚于快照、字段形状、完整快照与顺序、
  压缩后 brief 仍有计划、fast 不推、旧事件载荷不变）、规划重问、收尾 done/blocked、后来不合格的步骤不覆盖。
- golden 历史 `n04_plan_updated.json`（新路径）；`test_stage_replay` 核对之前录下的 golden 都没有 `n04-task-plan` 标记且仍能回放。
- 契约：`buf lint`、`buf format --diff --exit-code`、`buf breaking --against proto/breaking-baseline.binpb`、`buf generate`，
  `tests/contract` 的 schema 正反例与生成代码检查通过（`test_terminology` 的两处“账本”在 main 线的文件里，本线之前就失败）。
- 回归：patches_at_start、progress_events、task_workflow、task_verdicts、context_*、message_codes、task_guidance、replay 共 211 条通过。

## 没做的

- coding-web 的计划区（D 线）：读 `plan_updated` 或会话快照里的 `plan`，丢旧重建；`current_step` 高亮。
- light（Recipe）与保守规划不带步骤；要的话需要 Recipe 带步骤模板，另立项。
- 真模型下的规划质量与回退率未测（不花钱）。
