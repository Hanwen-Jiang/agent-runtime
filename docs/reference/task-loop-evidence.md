# Task Loop 的依据：框架、纠偏与 Triage

> 来源：`主文档` 第一、三、十节，`精简版` 第 1 节。已按设计书附录 A 改用新术语；旧叫法见 [运行核心速查](runtime-quickref.md) 第 7 节。
> 设计本身以[设计书](../design/agent-runtime-design.md) 3.2 与附录 B、C 为准。本文只保留“为什么这样设计”的证据和出处，供实现时判断边界情况。

## 1. 为什么用一个固定的 Task Loop

- **与主流生产系统同构**：
  - Magentic-One 的编排者先建任务账本，每步写进度账本，停滞时重新规划（[来源](https://microsoft.github.io/autogen/stable/reference/python/autogen_ext.teams.magentic_one.html)）。
  - Anthropic 的研究系统由主 agent 拆解并派生子代理，最后由引用 agent 核验来源（[来源](https://blog.bytebytego.com/p/how-anthropic-built-a-multi-agent)）。
- **比模式分类更贴近实现**：模式分类回答“有哪些做法”，固定框架回答“代码怎么写”。各模式都变成 Stage 里的配置，见[模式参考](patterns.md)。
- **两条约束仍然成立**：
  - 读可以并行，写保持单一写者（[Cognition 2026 生产经验](https://www.zenml.io/llmops-database/multi-agent-systems-in-production-code-generation-and-review-at-scale)）。
  - 多 agent 的 token 消耗约为普通对话的 15 倍（[来源](https://www.implicator.ai/inside-claudes-research-engine-are-teams-of-ai-agents-the-future-of-artificial-intelligence/)），所以 Goal Node 默认不派 sub_agent。
- **Stage 沿用 LangGraph 的超步语义**：同一超步内的节点并行执行，每个超步结束可以保存检查点（[来源](https://reference.langchain.com/javascript/langchain-langgraph/pregel/Pregel)）。本设计把它改名为 Stage。

## 2. 步长与切分

| 规则 | 依据或说明 |
| --- | --- |
| 每轮只规划到下一个信息检查点 | 凡是“后面的计划可能被新信息改变”的地方都切开；这是 Plan-and-Execute 与编排者-工人之间的折中 |
| 步长由不确定性决定 | 信息完整时一轮发完整 DAG（相当于 Plan-and-Execute）；部分未知时每轮一个阶段（相当于编排者-工人）；几乎未知时只发一个 Goal Node 去探索（ReAct） |
| 细循环放进 Node | tool_loop 的每一步是 Node 内的一次调用，不是一个 Round；否则开销和上下文都会失控 |
| 快速路径 | 简单任务里 Planner 与 Reviewer 退化为规则，不调用模型；对应 Triage 的 fast 与 light |
| 跨 Session 的长任务 | 就是跨 Session 的多轮循环：新 Session 的 Planner 从 Task State 接着做，即长时程 harness 的做法（[Anthropic](https://anthropic.com/engineering/effective-harnesses-for-long-running-agents)） |

## 3. 纠偏：Reviewer 的依据

### 3.1 Progress Check 的来源

Magentic-One 的做法（[来源](https://microsoft.github.io/autogen/stable/reference/python/autogen_ext.teams.magentic_one.html)）：

1. 编排者先把事实与合理猜测写进任务账本，据此规划。
2. 每一步生成进度账本，自查任务是否完成、是否有进展。
3. 连续足够多步没有进展，就更新任务账本、重新规划。

本设计对应为：Task State 中的 facts 与 assumptions，加上 Reviewer 每轮回答的五个 Progress Check 问题（附录 B.6）。

### 3.2 判定要独立，看产出物

- 进度评估最好交给独立上下文或另一个模型，避免自评宽容。
- 是否完成看产出物本身，不看对话记录（[harness 实践解读](https://www.mejba.me/blog/anthropic-long-running-agent-harness)）。
- 实现要求：Reviewer 的输入在代码里限定为 Stage 结果、Task State 与验收条件，不传 Planner 的规划过程。

### 3.3 升级顺序的依据

| 做法 | 依据 |
| --- | --- |
| replan 时把卡住的原因写进 Task State，让下一次尝试真正不同，而不是换个说法 | [双账本重规划实现](https://github.com/brcampidelli/chimera-agent/releases/tag/v0.3.0) |
| 问人时只给三种回复：原样继续、带指导重规划、停止并用现有结果收尾 | [Magentic 停滞干预决策](https://docs.rs/agent-framework-core/latest/agent_framework_core/workflow/enum.MagenticStallInterventionDecision.html) |
| 循环类 Goal Node 设最大迭代次数，检测并打断重复出现的同一错误 | [Ralph 反模式](https://notes.dsebastien.net/30+Areas/33+Permanent+notes/33.02+Content/Ralph+Wiggum+Technique) |
| 最常见的错误是用低层手段处理高层问题：计划本身错了，却在原地反复重试 | 主文档第三节的总结；设计书 3.2.5 的关键规则 |

调研文档里的“处置阶梯 L1–L7”与设计书 Verdict 的对应：

| 旧级别 | 含义 | 设计书 Verdict |
| --- | --- | --- |
| L1 局部重试 | 带错误信息重试，最多 2 到 3 次 | repair |
| L2 换方法 | 目标不变，换工具、换路径、拆小 Node | repair |
| L3 局部重规划 | 保留已验证 Node，重做剩余部分 | replan |
| L4 换模式 | 换 Stage 形状 | replan（换形状）或 reroute |
| L5 回滚 | 退回最近的检查点 | rollback |
| L6 升级 | 换更强的 Tier，或问人 | escalate |
| L7 体面终止 | 交付部分结果并说明原因 | abort |

## 4. Triage 的依据

- **业界做法一致**：GPT-5 的实时路由器按对话类型、复杂度、工具需求和显式意图，逐请求选择快模型或思考模型，并持续用真实信号训练路由器。真实信号包括用户切换模型、偏好和正确性（[来源](https://openai.com/index/gpt-5-system-card)）。
- **宁可轻规划加运行时纠偏，也不要每次重规划**：
  - 默认只做近乎零成本的分诊与差量检查。
  - 只有新任务族、分诊不确定、风险升级时，才做完整规划。
  - 判断偏了，由 Reviewer 的运行时信号纠正。
- **记录每次 Triage 决策与最终结果**：这些记录作为训练路由器和调整阈值的数据，思路与 GPT-5 路由器相同。

调研文档里的预检级别与设计书的对应：

| 旧级别 | 设计书 Triage 级别 |
| --- | --- |
| P0 直通 | fast |
| P1 轻预检（命中配方缓存） | light |
| P2 完整预检 | full |
| P3 运行中再预检 | replan |

## 5. 离线闭环

- **归因**：记录完整 trace，按 [MAST](https://arxiv.org/abs/2503.13657v2) 的三类对失败归类：规格问题、协作失配、验证缺失。
- **反哺**：失败模式用来更新路由规则、Model Profile 的阈值和 Node Template；有效的处置经验写入 failure 类记忆与 Recipe。
- **固化**：高频任务做成 Recipe；可以用 [AFlow](https://papers.lunadong.com/paper/7283) 式的搜索离线优化。
