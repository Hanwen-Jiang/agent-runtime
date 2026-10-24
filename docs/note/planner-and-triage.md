# Planner 与 Triage：分诊、规划与 Node Catalog

> 学习笔记，2026-10-24。对应代码：`services/py-worker/src/agentruntime_worker/planner/`（`planner.py` 分诊与规划、`prompts.py`、`validate.py` 校验、
> `context.py` 规划上下文、`activities.py` plan_round 与开发组装）；评测 `services/py-learning/src/agentruntime_learning/evals/`（`worker_subject:planner_subject`）。
> 设计依据：设计书 3.2.2，附录 C（Triage）、D.7（Node Catalog）、E.1；实施计划 S-09。

## 1. Planner 在 Round 里的位置

每个 Round 开头调一次 Planner（decision activity `plan_round`）。输入：Task State、用户消息与输入、上一轮的结果与 Verdict、当前的 Task Plan、
Session 上下文（Node Catalog、Recipe 缓存、effect 上限、已授权风险）。输出：本轮的 Stage（一个 DAG）、Triage 级别、Task Plan，以及这次规划的花费。

**一轮只规划到下一个信息检查点**：要看结果才能决定的后续步骤留到下一轮，不一次排满。

## 2. Triage：四个级别

| 级别 | 什么时候 | 调强模型吗 |
| --- | --- | --- |
| fast | 续改一类的简单请求（改标题、再短一点），effect 不超过已授权的 | 否 |
| light | 命中了任务族的 Recipe 缓存，风险不超过已授权的 | 否，按 Recipe 实例化 |
| full | 新任务、风险上升、置信度不够 | 是：完整规划（附录 C.5 的 11 个维度） |
| replan | Reviewer 判了 replan，或上一轮的假设被推翻 | 是，只重做剩余部分，保留已验证的 Node |

**规则优先**：分诊先用小模型（`small@low`）给出信号（请求类型、任务族、预计 effect、置信度），再由**代码**按规则定级：风险等级上升一律 full（附录 C.1），置信度低于 0.8 走 full，
命中 Recipe 且风险合格走 light。fast 与 light 不调强模型，增加的延迟控制在数百毫秒内（有测试量 Planner 自己的开销）。

## 3. 结构化输出与回退

- 输出必须通过 Stage 与 Task Plan 的 JSON Schema，再过代码校验（`validate.problems`）：DAG 无环、引用只指向祖先、写操作不挂 quality、
  **Atomic Node 的模板必须在 Node Catalog 里**（不能编造）、Goal Node 用的工具必须登记过、effect 不超过上限、replan 不重做要保留的 Node。
- 不合格时带上错误说明重问一次；还不行就**回退到保守规划**：单个 Goal Node、mode 为 tool_loop。
- 幂等键、seq 由代码重新生成，模型写的不算。

**Node Catalog 从哪来**：没接 Registry 时是固定的几个模板；接了 Registry 时按 Task 固定的版本列 Node Template 与当前的 Recipe。工具与 effect 取自 ToolDriver 的登记
（`ToolsContextSource`，T-13 写测试时发现之前这里是空的，真 Planner 根本规划不出用平台内工具的 tool_loop）。规划之后，每个 tool_loop 的 Goal Node 自动加上 `context_read`
（按路径取回被外置或遮蔽的内容）。

## 4. 成本

一次 full 规划约 1.25 万输入 token、2 千输出 token。按一方标价，strong 档约 0.09 美元，在评测集里预计成本 0.3 到 1.5 美元的 Task 上占比远超设计书的 5%。
改进方向：提示瘦身（Catalog 与 schema 占大头）、提示缓存（system 区末尾有断点）、换更便宜的强模型，或对小 Task 改用绝对额。

## 5. 评测（S-08、S-09）

评测集 64 条（开发集与留出集），标注经人确认；每条有期望的 Triage 级别、合格 Stage 的形状特征（是否扇出、有没有 gate）、期望的 Verdict。门槛与真模型结果
（2026-10-22，每个划分 3 遍取中位数）：

| 门槛 | 开发集 | 留出集 |
| --- | --- | --- |
| Triage 准确率 ≥ 90% | 88.5% ✗ | 85.7% ✗ |
| Stage schema 通过率 100% | ✓ | ✓ |
| 规则违例 0 | ✓ | ✓ |
| 规划成本 ≤ 5%（最大一条） | 17.1% ✗ | 37.3% ✗ |

Triage 的错误集中在：改标题一类的续改被判 full；有 Recipe 的新任务因为分诊给了 follow_up + effect none 被代码规则先判成 fast；置信度在 0.8 附近摇摆。

## 6. 面试速查

**Q：为什么不让一个大模型直接规划所有任务？**
A：大部分请求很简单或很常见，每次都调强模型做完整规划，成本和延迟都浪费。分诊用小模型加规则，简单的走 fast，常见的走 Recipe（light），只有新任务和高风险的才 full。

**Q：为什么风险判断交给代码而不是模型？**
A：风险上升必须走完整规划，这是安全规则，不能有“模型觉得没事”的例外。模型只给信号，定级由代码按规则做，可测、可审计。

**Q：模型输出的计划不合格怎么办？**
A：先按 schema 和代码规则校验，把错误原样告诉模型重问一次；还不行就回退到最保守的计划：一个 tool_loop 的 Goal Node，让执行时再一步步探索。

**Q：怎么防止模型编造不存在的模板或工具？**
A：Atomic Node 只能从 Node Catalog 实例化，模板引用、工具名在代码里逐个核对；接了 Registry 时执行前再按登记的模板核对 executor 与 effect。

**Q：replan 时怎么避免重做已经验证过的部分？**
A：Reviewer 判 replan 时给出要保留的 Node；Planner 的输入带上一轮的结果，校验时检查新 Stage 里没有这些 Node，下游直接引用它们的结果。

**Q：评测集怎么防止被“调过拟合”？**
A：分开发集和留出集，提示只在开发集上调，留出集只看汇总、记看过几次；标注要经人确认；真模型有随机性，每次跑 3 遍取中位数，报告保留单次结果。
