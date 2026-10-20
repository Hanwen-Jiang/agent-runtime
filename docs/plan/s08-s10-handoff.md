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
| S-08 的提交 | 评测集与评测 harness，见下 | S-09、S-10 用它评测 |

## 主干这边的做法

每个工作项按 CLAUDE.md 的流程：先写验收测试并等人确认，再实现，最后列出没覆盖的地方。进度与关键决定记在下表，提交信息里有细节。

| 工作项 | 状态 | 关键决定 |
| --- | --- | --- |
| S-08 | 已实现，等人审评测集 | 见下一节 |
| S-09 | 未开始 | — |
| S-10 | 未开始 | — |

## S-08 做成了什么样（S-09、S-10 会用到）

- 代码在 `services/py-learning`（`agentruntime_learning.evals`），评测集在 `services/py-learning/evalsets/task-loop`：39 条 Task 用例、24 条 Reviewer 专用样本，每条一个 JSON，格式见 `case.schema.json`，每条带人确认过的参照输出。标注目前都是草稿（`review.status = draft`），等人审。
- 一条命令跑评测：`uv run --package agentruntime-learning python -m agentruntime_learning.evals run --subject <被测对象>`。S-09、S-10 的 Planner 与 Reviewer 以 `<模块>:<工厂函数>` 接入，实现 `model.Subject`（`plan` 与 `review` 两个方法）。
- Reviewer 的输入只有 Stage、Stage 结果、Task State 与之前各 Round 的摘要（`ReviewerInput`），没有 Planner 的规划过程；被测对象也拿不到用例 id。
- Verdict 序列按 Round 逐个判定点评测（用标注的 Stage 与结果），不是整轮实跑。
- 规则由代码判定：风险上升必须 full；不可逆动作必须是按 `write_irreversible` 声明的 Atomic Node；模板、Recipe、工具必须在 Catalog 里；effect 不超过上限、不低于模板；fast 与 light 不调用强模型；light 必须命中最近没失败的 Recipe；done 必须来自成功的 deliver 节点。
- 每个判定点标了“至少升级到哪一步”（附录 B.8），低于它还在 repair 记为“该升级却原地重试”（S-10 验收要求为 0）。
- 留出集只给汇总分数；提示只在开发集上调（有测试检查留出集内容没有进 Worker 的代码与提示）。
- 基线（永远 full + 单个 Node）在开发集上成功率 4%，报告列出失败原因；参照输出在两个划分上都是满分。

## 需要你配合的

- 不要在 S-08 至 S-10 的范围里另起实现，以免两边冲突；有想法请直接提给项目负责人。
- 核心链路（Task Loop → Stage Runner → Node Executor）同一时间只由一个 agent 修改（CLAUDE.md），S-09、S-10 会改到 Task 工作流调用的 decision activity。
- 做 S-08 时发现：Stage Runner 只在 `write_irreversible` 的 Atomic Node 前插入人工审批，tool_loop 里调用不可逆工具不经过审批。已作为问题报给项目负责人；在修好之前，规划时不要把不可逆工具交给 Goal Node。
