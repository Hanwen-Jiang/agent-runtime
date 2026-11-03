# N-16 多 Agent 实施记录（2026-11-03，E 线）

对应 [feedback-2026-11-01.md](feedback-2026-11-01.md) P2 的 N-16、设计书 3.2.3 与附录 E.1（sub_agent 独立上下文、只交回摘要、默认只读；
嵌套深度不超过 2；共享产物单写者）、人工验收基线 Q74（子 Agent 权限默认继承、不能提升），契约清单
`tests/contract/fixtures/n15_n16_contract_cases.json` 的 N-16 部分与 [n15-n16-contract-baseline-2026-11-02.md](n15-n16-contract-baseline-2026-11-02.md)。

## 已有的（不改）

- sub_agent 是 `SubAgentWorkflow` 子工作流里跑的 tool_loop，同一 Stage 里互不依赖的 sub_agent 本来就并发（拿执行名额，受
  `StagePolicy.max_parallel` 限制）；结果按 Node id 排序合并；取消父 Task 会取消正在跑的子工作流；sub_task 深度不超过 2；
  sub_agent 的子工作流里不能再派 sub_task（深度按上限算）；写明了高于 Task 上限的 effect 由 `_run_node` 拒绝。

## 契约（CONTRACT DRAFT 提交，待人审）

- `node_started` 加可选的 `mode`（Goal Node 的展开方式，没写时按 Stage Runner 的规则解析）。
- `tool_status` 的说明：顶层 sub_agent 在自己的子工作流里的调用也推，`node_id` 是那个 sub_agent；并行的几个交错到达、按
  event_id 排。sub_task、子图等其余子工作流仍不推。没有 proto 改动。

## 实现（版本分支 `n16-sub-agents`，追加在 `AT_START` 末尾）

- **进度**：顶层 sub_agent 的子工作流带自己的 `EventPublisher`（`SubAgentInput.publish_events`，之前的输入没有，默认不推），推
  `tool_status`、`context_usage`，去重键里有 sub_agent 的 Node id，不会和别的 Node 撞。界面（D 线）按 `node_started.mode == sub_agent`
  开一条子 Agent 的进度，之后 node_id 相同的 `tool_status` 进这一条，`node_finished` 收尾；要的事件就这三种，不需要新的事件类型。
- **分工**：一个 Stage 最多 `MAX_SUB_AGENTS = 4` 个 sub_agent（`stage_plan.validate_stage` 的 `max_sub_agents`）；Planner 的检查
  不合格就重问，Stage Runner 在执行前整个 Stage 拒绝（一个子 Agent 都不派），不是排队。Planner 规则 11：只给读密集、宽度型、
  能拆成互不重叠的几块的工作用；各自在 goal、inputs 写清范围、只读；由一个下游节点 after 它们、引用各自的结果合并。
- **权限（Q74）**：sub_agent 没写 `effect_ceiling` 时继承 `min(read, Task 的上限)`（之前固定 read，Task 上限是 none 时被拒）；
  写明更高的、给写工具的照旧拒绝。sub_task 的子 Task 预算压到父 Task 剩下的（美元、工具调用次数、时间），用完了不再派生
  （`budget_exhausted`）；之前子 Task 拿到的是父 Task 的整份预算，节点写的美元数也可以更高。
- **合并**：不变（按 Node id 排序，与完成先后无关），加了测试。

## 决策表

| 项目 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 子 Agent 的进度 | 界面只看到 sub_agent 开始与结束，中间是黑的 | 契约原文“sub_agent 等子工作流里的调用不推” | 子工作流用同一个事件通道、同一个 Task 的 event_id 序列 | 并行的子 Agent 各有进度 | 每次工具调用多两个事件 | 低 | 无 | 子工作流推送失败只影响它自己的这一轮进度 | N-16 | KEEP |
| 个数上限 | 无界派生，成本失控 | 附录 E.1：多 agent 的 token 消耗远高于单 agent；清单 tenant_agent_cap_is_enforced | 每个 Stage 至多 4 个，超出整个 Stage 拒绝 | 成本有界、可诊断 | 无 | 低 | 上限是常量，要改就改代码 | 合理的 5 路拆分要分两轮 | N-16 | KEEP |
| 权限继承 | 子 Agent 拿到比主任务更高的权限或预算 | Q74；sub_task 原来复制父 Task 的整份预算 | 缺省继承低者；预算压到剩余 | 不能借子 Task 绕过上限 | 纯计算 | 低 | 无 | 剩余预算很少时子 Task 更早停 | N-16 | KEEP |
| 共享产物写者 | — | sub_agent 只读，sub_task 持写锁 | 不变 | — | — | — | — | — | — | 已有，不改 |
| 新的 agent 状态机 | 两套状态分裂 | 基线：复用 Task/Workflow/Node | 不建 | — | — | — | — | — | — | REJECT |

## 验证

- `tests/test_n16_sub_agents.py` 8 条：两个子 Agent 并行（一个停住时另一个照样跑完）与按 id 合并、各自的进度由各自的子工作流
  推送且落在 node_started 与 node_finished 之间、超过上限整个 Stage 拒绝、Planner 重问、不能提升 effect 与不能给写工具、
  继承更低的 Task 上限、sub_task 不能高于父 Task 且预算压到剩余、取消传到子 Agent。
- golden 历史 `n16_sub_agent_events.json`（SubAgentWorkflow 的新路径）；之前录下的 golden 都没有 `n16-sub-agents` 标记且仍能回放。
- `test_progress_events.py` 两条按新的契约更新（Goal Node 的 node_started 带 mode；顶层 sub_agent 的子工作流推送）。

## 没做的

- 界面（D 线）：子 Agent 的并行进度条。
- 子 Agent 的嵌套深度、取消、重复完成的幂等由 Temporal 子工作流保证，没有另加；跨 Task 读写的拒绝沿用 context_read 与工具的作用域（R-03）。
- 真模型下 Planner 是否会恰当拆分没有评测（不花钱）。
