# 记忆与检索

> 来源：`主文档` 第六、七节。已按设计书附录 A 改用新术语：晋升 → Promotion，flow 配方 → Recipe，S1 稳定区 → profile 区。
> 设计以[设计书](../design/agent-runtime-design.md) 3.4 与附录 I 为准。本文保留业界方案对比、研究证据与安全案例，供实现 Memory Service、S-12b 与 S-13 时查阅。

## 1. 两类记忆

现有方案分两类：

- **关于用户和世界的事实**：偏好、档案。
- **怎么做事的经验**：策略、教训、流程。

后者是 agent 越用越强的关键，也最容易被污染。本设计用 kind 区分：preference、fact 属于前一类；success、failure、recipe 属于后一类（附录 I.2）。

## 2. 记忆框架与产品

| 方案 | 核心机制 | 作用域 | 本设计借鉴了什么 |
| --- | --- | --- | --- |
| [Mem0](https://docs.mem0.ai/platform/features/entity-scoped-memory.md) | LLM 抽取事实；已有语义等价的记忆时更新而不是新增 | user、agent、app、run 四种实体 ID；多实体写入会拆成每条只属于一个主实体的记录，以守住隐私边界 | 写入最小必要作用域；“更新优先于追加” |
| [Zep / Graphiti](https://signals.aktagon.com/articles/2026/07/zep-a-temporal-knowledge-graph-architecture-for-agent-memory/) | 时序知识图谱，分情节、实体、社区三层；双时间轴记录事件时间与写入时间；新旧事实矛盾时给旧边打失效时间，不删除 | 用户或群组 | Memory Item 的 validity：用有效期，不覆盖 |
| [Letta（原 MemGPT）](https://docs.letta.com/guides/agents/architectures/sleeptime/index.md) | 上下文内可编辑的记忆块 + 外部归档；后台的 sleep-time agent 默认每 5 步整理一次主 agent 的记忆块；记忆可存为 git 版本化的文件系统 | agent，多 agent 可共享块 | 写入流水线异步化、离开关键路径；记忆库版本化、可回滚 |
| [LangMem](https://www.langchain.com/blog/langmem-sdk-launch) | 语义（事实）、情节（过往经历）、程序（行为规则）三类；程序记忆通过提示优化器改写指令 | 命名空间像目录，通常带 user_id 防串号 | 类型划分；层级作用域 |
| [Claude memory tool](https://platform.claude.com/en/docs/agents-and-tools/tool-use/memory-tool) | 客户端文件工具：模型在 /memories 下增删改查，应用把路径映射到真实存储（如每用户一个目录或数据库键）；任务开始前先查看记忆目录 | 由应用决定 | 存储与隔离由平台掌控；需要防路径穿越 |

Claude 的 memory tool 还可以与 context editing 配合：上下文接近清理阈值时，模型会收到提醒，先把工具结果里的重要信息存进记忆文件，再清理旧结果（[来源](https://docs.claude.com/zh-TW/docs/agents-and-tools/tool-use/memory-tool)）。这正是“工作记忆”与“长期记忆”之间的交接点，对应 Context Builder 的 before_compact Hook（附录 F.1）。

## 3. 经验学习研究

| 研究 | 做法 | 结果 | 对实现的要求 |
| --- | --- | --- | --- |
| [ReasoningBank](https://research.google/blog/reasoningbank-enabling-agents-to-learn-from-experience/)（Google，ICLR 2026） | 从自评的成功与失败轨迹中提炼可泛化的策略条目，而不是保存原始轨迹 | 相比无记忆 agent，WebArena 成功率 +8.3%，SWE-bench Verified +4.6%；优于只存原始轨迹或只存成功流程 | failure 与 success 一样要提炼；存策略，不存录像 |
| [ACE](https://iclr.cc/virtual/2026/10018667)（ICLR 2026） | 把上下文当作持续演化的 playbook，由生成、反思、策展三个角色维护；结构化的条目级增量更新 | 不需要标注，靠执行反馈学习；agent 任务 +10.6%，金融任务 +8.6% | 经验库只做条目级增删改，绝不整体重写；定期语义去重 |

ACE 指出整体重写有两种失败：简洁偏置（为了精炼丢掉领域细节），以及上下文坍缩（反复重写导致细节流失）。

## 4. 安全：记忆投毒

记忆把一次性的提示注入变成了持久化攻击。

| 案例 | 内容 |
| --- | --- |
| [MINJA](https://arxiv.org/abs/2601.05504v1) | 普通用户仅通过查询，就能向共享记忆注入恶意记录；理想条件下注入成功率超过 95%，攻击成功率约 70%。后续研究指出，现实部署中这些数字会下降，且清洗策略需要仔细校准信任阈值 |
| [MemGhost](https://forkast.news/?p=127766)（2026-07 披露） | 一封构造好的邮件，就能让 agent 把恶意指令写进 MEMORY.md 一类的持久文件 |

对多租户的直接含义：Workspace 级与 Platform 级的共享经验库是跨用户的攻击面，写入必须带来源、信任分和审核流程。对策落在 S-13（[强档计划](../plan/strong-tier-plan.md)）：

- 来源分级；外部内容来源的条目不可晋升。
- 指令式条目必须人审。
- 与已有高信任条目语义冲突的新条目进入人审。
- 监控异常：hurt 突增、某个条目突然被大量检索。
- 记忆库版本化，可回滚。

## 5. 向量检索（RAG）

向量检索只是记忆召回的一路，不是全部：服务端 ACL 与结构化过滤在前，稠密与稀疏混合召回在中，重排在后，最后按信任、新鲜度和作用域融合打分。流水线见附录 I.6。

### 5.1 调研依据

| 结论 | 证据 |
| --- | --- |
| 混合优于单路 | 2026 年生产 RAG 的主流做法：稠密检索与词法检索用 RRF 融合，再用 cross-encoder 对前 50 个候选重排（[来源](https://jacar.es/en/hybrid-rag-in-2026-the-patterns-that-keep-winning/)）。RRF 只看排名，不需要把两种分数归一化，常用常数 k = 60（[来源](https://www.codingshuttle.com/blogs/building-production-ready-rag-hybrid-search-reranking-and-conversational-memory)） |
| 上下文化索引有效 | Anthropic 的 Contextual Retrieval 先给每个片段加一段 LLM 生成的上下文说明，再同时做嵌入与 BM25 索引。前 20 个片段的检索失败率从 5.7% 降到 2.9%，加上重排降到 1.9%（[来源](https://getcoai.com/?p=21330)）。设计书附录 I.6 对 Workspace 知识采用“片段 + 上下文前缀” |
| 多租户隔离有两种形态 | 每个租户一个独立集合或命名空间：隔离强，但数量受限（Qdrant Cloud 默认每集群 1,000 个集合）。单集合按租户字段分区、每次查询都带租户过滤：这是 Qdrant 推荐的默认做法（[来源](https://oneuptime.com/blog/post/2026-08-28-qdrant-one-collection-per-tenant-or-payload-partitioning/markdown)）。Pinecone 用 namespace；Weaviate 原生多租户为每个租户分一个分片（[来源](https://particula.tech/blog/multi-tenant-rag-isolation-silo-pool-bridge)） |
| 嵌入可被反演 | Vec2Text 能精确恢复 92% 的 32 token 输入，并从临床记录的嵌入中恢复 89% 的人名；嵌入应与原文同等保护（[来源](https://arxiv.org/pdf/2310.06816)） |
| 代码不必建向量索引 | Claude Code 只预加载少量说明文件，其余用 glob、grep 按需检索，避免索引过期（[来源](https://agentic-ai.readthedocs.io/en/latest/ContextEngineering/anthropic/)） |

### 5.2 多租户索引布局

- 默认每个租户一个逻辑分区：单集合 + 租户字段索引。大租户或合规要求高的租户，升级为独立集合，乃至独立集群与独立密钥。
- Workspace、User 作为分区内的过滤字段；Platform 级的通用经验放在独立的只读索引。
- ACL 过滤条件由服务端根据认证身份拼接，永远不由模型生成；缺少租户条件的查询直接拒绝。
- 定期做越权测试：以 A 租户身份查询，断言返回的 B 租户结果为零。这是 S-12 的验收方式。

### 5.3 查询构造

| 时机 | 查询内容 |
| --- | --- |
| Triage 时 | 任务描述 + 任务族 |
| Node 开始时 | 节点目标 + 工具 |
| 出错时 | 错误签名（精确匹配）+ 错误消息（语义匹配）；这一路收益最高 |

可以由小模型改写出 2 到 3 个查询变体。

### 5.4 写入、更新与迁移

- 条目更新时，按同一 ID 重写向量。
- 删除时写墓碑，同步删除向量与稀疏索引条目，并沿 lineage 级联。
- 嵌入模型升级时，按 embedding_version 双写，后台重嵌入；影子比对召回率达标后，再切换读路径。
- 与写入流水线共用安全过滤：PII 先脱敏，再嵌入。

### 5.5 评测

- **离线**：从 trace 构造“查询 → 应命中条目”的标注集，分别测召回段与重排段的 recall@10、nDCG@10。先保召回，因为重排救不回没被召回的候选。
- **在线**：检索采用率、helped 与 hurt、空召回率、P95 延迟。
- **模型选择**：嵌入与重排模型可以先用公开榜单初筛，最终以自有标注集为准。

## 6. 开放问题

以下问题需要团队决定，也列在[风险与开放问题](risks-and-open-questions.md)：

- Workspace 级经验晋升是否必须人工确认，还是达到验证次数即自动生效。
- 是否允许去标识化后的通用经验跨租户共享，默认开还是关。
- 用户能否看到 Workspace 经验的来源成员。
