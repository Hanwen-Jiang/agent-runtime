# 上下文与压缩

> 来源：`主文档` 第八、九节。已按设计书附录 A 改用新术语：分区 S0–S4 → system、profile、brief、history、status；压缩阶梯 L0–L4 → offload、mask、prune、summarize、reset。
> 设计以[设计书](../design/agent-runtime-design.md) 3.2.7 与附录 G 为准。本文保留调研依据与实证对比，供实现 Context Builder、S-11 与 R-03 时查阅。

## 1. 为什么不能靠大窗口

- Chroma 在 2025 年测试了 18 个前沿模型，全部随输入变长而退化，即使任务很简单（[来源](https://trychroma.com/research/context-rot)）。
- 语义相近的干扰项和文档结构的变化，都会明显拉低表现。
- 大海捞针（NIAH）类测试的高分，掩盖了真实长上下文任务的难度。

结论：**标称窗口不等于有效窗口**。Model Profile 要记录实测的有效长度，压缩阈值按它来设。

## 2. 三家实践

| 来源 | 做法 | 本设计采用的部分 |
| --- | --- | --- |
| Anthropic | 即时检索：只预加载少量说明文件，其余用 glob、grep 等工具按需获取。长任务三招：接近上限时压缩后重开；把笔记与待办写到上下文之外；用子代理隔离上下文（[来源](https://agentic-ai.readthedocs.io/en/latest/ContextEngineering/anthropic/)）。压缩提示应先调召回再调精度，因为过度摘要会丢掉后来才显出重要性的细节（[来源](https://rubinlake.com/it/technology-radar/developer-ai-and-delivery/context-engineering)） | 外置内容只以路径出现；reset；sub_agent；S-11 的调参顺序 |
| Manus | 把 KV 缓存命中率视为生产 agent 最重要的指标：保持前缀稳定，系统提示里连秒级时间戳都不放；工具定义不动态增删，改用解码时屏蔽；把文件系统当作无限、持久、agent 可直接操作的外部上下文（[来源](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)）。压缩是可恢复的：删掉文档内容但保留路径；todo.md 每步复述到上下文末尾。代价：早期由主 agent 维护 todo.md 时，约三分之一的动作花在了记账上（[来源](https://tianpan.co/blog/2026-03-02-context-engineering-lessons-from-manus)） | 分区按稳定性排序；工具集固定；status 区放末尾，由 Worker 生成，不让模型自己维护 |
| 平台原生能力 | Claude API 把服务端压缩作为长对话的主策略，另提供上下文编辑：工具结果清理默认在 10 万输入 token 时触发，被清理处留占位符；思考块可按轮次保留，保留时不破坏提示缓存（[来源](https://platform.claude.com/en/docs/build-with-claude/context-editing)）。OpenAI 提供 /responses/compact 接口 | 有原生能力时，mask、prune、reset 交给原生能力；自研只负责必须保留的清单与状态注入（附录 G.5） |

## 3. 压缩方法的实证对比

| 方法 | 做法 | 证据 | 风险 | 本设计中的位置 |
| --- | --- | --- | --- | --- |
| 观察遮蔽 | 只把较早的工具输出替换为占位符，保留推理与动作 | JetBrains 在 SWE-bench Verified 上测了 5 种模型配置：成本约为不做管理的一半，解决率与 LLM 摘要持平甚至略高；二者混合再降本 7% 到 11%（[来源](https://arxiv.org/pdf/2508.21433)） | 依赖被遮蔽的内容能重新获取 | mask |
| 锚定迭代摘要 | 固定分区的持久摘要（意图、文件改动、决策、下一步）；只摘要新截断的片段，再合并进去 | Factory 用 36,000+ 条生产消息做探针问答：相近压缩率下，保留的“可继续任务”信息多于 OpenAI compact 与 Anthropic SDK 的压缩（[来源](https://docs.factory.ai/guides/power-user/evaluating-context-compression)） | 文件改动轨迹仍然弱，最好成绩也只有 2.45/5 | summarize |
| 全量摘要重写 | 每次触发都重新生成整份摘要 | ACE 指出，反复整体重写会造成细节流失的“上下文坍缩” | 多轮后漂移、丢细节 | 不采用 |
| 外部笔记与账本 | 进度、决策、待办写入结构化文件 | Anthropic、Manus 的共同做法 | 记账开销侵占有效动作 | Task State；status 区由 Worker 生成 |
| 子代理隔离 | 子代理用独立上下文工作，只交回摘要 | 深度研究类做法 | 交接时丢信息 | sub_agent；offload |
| 新上下文重启 | 状态写盘后，开新上下文接着做 | 长时程 harness、Ralph 循环 | 成本高，依赖交接质量 | reset |

两条对设计最关键的结论：

1. **精确状态不能交给摘要**：文件与产物轨迹这类状态，要由 Runtime 用结构化数据单独跟踪，也就是 Task State。
2. **能重新获取的内容优先遮蔽而不是摘要**：便宜，而且不会产生幻觉。

## 4. 速查：分区与压缩阶梯

| 分区 | 内容 | 稳定周期 | 压缩方式 |
| --- | --- | --- | --- |
| system | 平台提示、固定工具集定义、租户策略 | 跨 Session | 不压缩；缓存断点 1 |
| profile | 用户偏好、Workspace 约定（来自 Memory） | Session 内 | 不改写，新偏好以变更形式追加；缓存断点 2 |
| brief | Task Plan：目标、验收条件、约束、计划摘要、检索到的经验 | Task 内 | replan 时整体替换；缓存断点 3 |
| history | 对话与工具调用轨迹，只追加 | 每步增长 | 压缩的主要对象 |
| status | 进度、待办、下一步，放在最末尾 | 每步更新 | 保持在约 500 token 以内 |

压缩阶梯从便宜到昂贵逐级触发：

1. **offload**：大工具输出直接写入 ObjectStore，只回摘要与路径。
2. **mask**：较早的工具结果替换为占位符和引用。
3. **prune**：清理旧轮次的思考内容。
4. **summarize**：锚定式增量摘要。
5. **reset**：用 system、profile、brief、持久摘要与 status 开一个新上下文。

参考阈值：有效长度的 40% 开始 mask，70% 开始 summarize；出现重复或停滞信号时 reset。按工作模式、目标与模型的差异化规则，见设计书附录 G.3–G.5。

## 5. 评测

- **Compaction Probe**：每次 summarize 或 reset 后，抽 3 到 5 个关键事实提问；答错就补回原文片段。
- **跟踪指标**：压缩后的 Task 成功率；压缩后的重复操作率，即重读同一文件、重做已完成步骤；缓存命中率；每个 Task 的 token。
- **基线**：S-11 的验收以“只做 mask”为基线。依据是 JetBrains 的结果：遮蔽单独就能达到与摘要相当的效果。
