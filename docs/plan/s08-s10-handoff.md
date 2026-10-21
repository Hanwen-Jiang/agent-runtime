# S-08 至 S-10 接手说明（临时文档）

> 这是临时文档：S-10 合入主干、负责 S-08 至 S-10 的同事看过之后删除。

## 为什么接手

- 2026-10-20 起，S-08（评测集与评测 harness）、S-09（Planner 与 Triage）、S-10（Reviewer 判定）改由主干这边接手，按计划顺序做。
- S-12b、S-13 依赖 Memory Service 主体（中档工作项，还没有开始），按计划也排在阶段 3、4 之后，所以先不做。
- 接手前看过 `fork/s08-s10`：分支 `s08-s10` 停在 `f5d8878`（主干的 S-06），工作区没有改动；GitHub 上也没有对应的分支或 PR。
  **如果你在别处已经有 S-08 至 S-10 的设计或代码，请尽快推到一个分支并告知项目负责人**，我们以你的成果为起点评审、合并，不重写。

## 你的 fork 之后主干上的变化

| 提交 | 内容 | 对 S-08 至 S-10 的影响 |
| --- | --- | --- |
| `eafac66` | S-12a：Postgres 业务库的行级安全（`libs/go-core/adapters/postgres`）；task-service 设了 `AR_RELATIONAL_DSN` 时用 Postgres；夜间任务加了 Go 测试与 Postgres 上的端到端测试 | 新的业务表不能用外键，主键与唯一约束会被改写成以 `tenant_id` 开头 |
| `0a014a7` | 设计书同步原文档 rev 11：4.3 补“行级安全的约束”；本说明 | 同上 |
| `905a6fd` 起 | S-08 评测集与评测 harness，见下 | S-09、S-10 用它评测 |
| S-08 人审之后的提交 | 设计书同步原文档 rev 21：附录 B.7、C.1、C.2、D.5、D.6 统一口径 | Triage：风险按本 Session 已授权的等级比较，已授权的写可以 light，light 涉及不可逆动作要多一项检查；续改开新 Task 并继承上一个 Task 的 State；任务本身需要的人工确认由 Planner 写成 human 的 gate 节点 |
| `1b3fcc9` | 设计书同步原文档 rev 40：按 D1–D16 设计缺口评审统一口径 | tool_loop 里每次调用不可逆工具前逐次人工审批；只读 Task 的 effect 上限压到 read；Task 提交时固定一个 Registry 修订号；发布门槛改为非劣效检验 |
| `e191089` | S-08 人审第二轮：17 条确认，两条待定 | 见下表 |
| D16 的提交 | Stage Runner 在 tool_loop 里每次调用不可逆工具前问人；`call_tool` 再查一次审批记录；S-08 评分器相应调整 | Goal Node 可以带不可逆工具，运行时逐次审批 |
| S-10 的提交 | 设计书同步原文档 rev 44：3.2.5 Reviewer 的输入含本轮执行的 Stage；Reviewer、Verdict 去向与收尾写回，见下 | Task 工作流的 Round 循环与收尾顺序换到版本分支 `s10-reviewer` 后面 |

## 主干这边的做法

每个工作项按 CLAUDE.md 的流程：先写验收测试并等人确认，再实现，最后列出没覆盖的地方。进度与关键决定记在下表，提交信息里有细节。

| 工作项 | 状态 | 关键决定 |
| --- | --- | --- |
| S-08 | 已完成：人审两轮后 64 条标注全部确认，S-09、S-10 的门槛可以用 `--require-confirmed` | 见下一节 |
| S-09 | 已实现（假模型上的验收全部通过）；真模型上的跑分等 Model Gateway 接好；6 条 replan 用例补了上一轮并已复审确认 | Triage 由代码判定，小模型只给信号；full 与 replan 一次强模型调用，代码校验后重问一次再回退；Task 工作流把上一轮与 Task Plan 交给下一轮的 Planner |
| S-10 | 已实现（假模型上的验收全部通过，含震荡测试）；真模型上的跑分等 Model Gateway 接好 | 规则先于模型，代码判定 floor 与各级次数上限；Reviewer 另看本轮执行的 Stage 与之前各 Round 的摘要；原地重试与换更强的 Tier 不回 Planner、只重跑出问题的部分；先交付再异步写回 |

## S-08 做成了什么样（S-09、S-10 会用到）

- 代码在 `services/py-learning`（`agentruntime_learning.evals`），评测集在 `services/py-learning/evalsets/task-loop`：40 条 Task 用例、24 条 Reviewer 专用样本，每条一个 JSON，格式见 `case.schema.json`，每条带人确认过的参照输出。人审结果写在每条的 `review` 里（`confirmed` 或 `draft` 加修改说明）。
- 一条命令跑评测：`uv run --package agentruntime-learning python -m agentruntime_learning.evals run --subject <被测对象>`。S-09、S-10 的 Planner 与 Reviewer 以 `<模块>:<工厂函数>` 接入，实现 `model.Subject`（`plan` 与 `review` 两个方法）。
- Reviewer 的输入只有 Stage、Stage 结果、Task State 与之前各 Round 的摘要（`ReviewerInput`），没有 Planner 的规划过程；被测对象也拿不到用例 id。
- Verdict 序列按 Round 逐个判定点评测（用标注的 Stage 与结果），不是整轮实跑。
- 规则由代码判定：风险上升必须 full；Atomic Node 的不可逆动作必须按 `write_irreversible` 声明（Goal Node 里的不可逆工具由运行时逐次审批）；Goal Node 的工具不能高于它自己的 effect 上限；模板、Recipe、工具必须在 Catalog 里；effect 不超过上限、不低于模板；fast 与 light 不调用强模型；light 必须命中最近没失败的 Recipe；done 必须来自成功的 deliver 节点。
- 每个判定点标了“至少升级到哪一步”（附录 B.8），低于它还在 repair 记为“该升级却原地重试”（S-10 验收要求为 0）。
- 留出集只给汇总分数；提示只在开发集上调（有测试检查留出集内容没有进 Worker 的代码与提示）。
- 基线（永远 full + 单个 Node）在开发集上成功率 4%，报告列出失败原因；参照输出在两个划分上都是满分。

## S-09 做成了什么样（S-10 会用到）

- 代码在 `services/py-worker/src/agentruntime_worker/planner`；Worker 设 `AR_PLANNER=model` 时由它接管 `plan_round`，默认仍是桩 Planner。
- Triage 由代码按附录 C.1、C.2 判定，小模型分类器只给信号；fast 与 light 不调用强模型；full 与 replan 一次强模型调用，代码校验后重问一次，仍不合格回退到一个 tool_loop 的 Goal Node。
- Task 工作流把上一轮（Stage、结果与 Verdict）与 Task Plan 交给下一轮的 Planner（`PlanRoundInput.last_round`、`task_plan`）；只改 activity 的输入，不改命令序列。S-10 的 Reviewer 判 replan 时，`replan.keep_nodes` 就是 Planner 不再重做的 Node。
- 评测：`--subject agentruntime_learning.evals.worker_subject:planner_subject`，真模型经 `AR_EVAL_GATEWAY_FACTORY` 接入；在那之前假模型上能证明的是“结构与规则由代码兜底”。

## S-10 做成了什么样

- 代码在 `services/py-worker/src/agentruntime_worker/reviewer`；Worker 设 `AR_REVIEWER=model` 时由它接管 `review_round` 与 `finalize_task`，默认仍是桩。
- 一次评审：代码先算硬信号与升级台账（`signals`），再算“至少要升级到哪一步”（`rules.floor`）；Progress Check 只调用一次强模型（独立上下文，材料按白名单取出，见 `prompts.review_view`），不合格重问一次，仍不合格只按规则判定；Triage 为 fast 的 Round 不调用模型。
- 规则（附录 B.8）：有 Node 失败就不能 continue 或 done；同一 Node 失败 2 次换方法、3 次 replan；被拒绝的动作不原地重试；replan 2 次没有进展就换形状；预算用掉 80% 而完成度不到一半只能缩小范围、问人或收尾，用完只能问人或收尾。各级有次数上限（上次进展之后：原地重试 2、换方法 2、replan 2；整个 Task：换形状或 reroute 1、rollback 1、换更强的 Tier 1、问人 2），一直失败的 Task 在 12 次评审之内收尾。
- Task 工作流（版本分支 `s10-reviewer`）按 Verdict 的去向走：原地重试与换更强的 Tier 重跑同一个 Stage 里出问题的部分，成功的 Node 结果沿用；问人经 HumanChannel，回复限定三种，超时按停止收尾；rollback 退回检查点时的 Task State；reroute 换 Recipe 回 Planner；abort 交付部分结果。Reviewer 的汇总（事实、假设、产物）写进 Task State，Round 记下成本。
- 收尾先通知 Session 再写回：归档 Task State、经验与 Recipe 效果候选交给 Memory Service（接好之前记在进程内）、成本与 Verdict 分布写进 trace。
- 评测：`--subject agentruntime_learning.evals.worker_subject:worker_subject`（Planner 加 Reviewer）。

## 需要你配合的

- 不要在 S-08 至 S-10 的范围里另起实现，以免两边冲突；有想法请直接提给项目负责人。
- 核心链路（Task Loop → Stage Runner → Node Executor）同一时间只由一个 agent 修改（CLAUDE.md），S-09、S-10 会改到 Task 工作流调用的 decision activity。
- 做 S-08 时发现：Stage Runner 只在 `write_irreversible` 的 Atomic Node 前插入人工审批，tool_loop 里调用不可逆工具不经过审批。已按 D16 修好：每次调用不可逆工具前逐次人工审批，拒绝时把指导送回循环，同一 Goal Node 被拒 2 次记为 denied（设计书 3.2.3、附录 E.1）。
