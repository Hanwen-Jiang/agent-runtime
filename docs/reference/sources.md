# 参考来源总表

> 来源：合并 `主文档`、`模式图谱`、`精简版` 三份文档的来源列表，按主题去重。
> 各模式的论文出处（CoT、ReAct、Self-Refine、ToT 等）不在这里重复，见[模式参考](patterns.md)第 3 节的表格。
> 链接是调研时（2026 年 10 月）记录的，部分是二手解读或汇总页；引用具体数字前，请回到一手来源核对。

## 1. 模式分类与叫法

对应 [patterns.md](patterns.md)。

- [Andrew Ng 四种模式（Augment Code 汇总）](https://www.augmentcode.com/guides/agentic-design-patterns)
- [Anthropic: Building Effective Agents（Simon Willison 摘要）](https://simonwillison.net/2024/Dec/20/building-effective-agents)
- [Anthropic 五种 workflow 模式汇总](https://lyzr.ai/?p=56328)
- [Google 八种多 agent 设计模式（InfoQ）](https://infoq.com/news/2026/01/multi-agent-design-patterns)
- [Developer's guide to multi-agent patterns in ADK](https://developers.googleblog.com/en/developers-guide-to-multi-agent-patterns-in-adk/)
- [Antonio Gullí: Agentic Design Patterns](https://www.ebooks.com/en-ai/book/347123043/agentic-design-patterns/antonio-gull/)
- [LangGraph: Multi-Agent Workflows](https://www.langchain.com/blog/langgraph-multi-agent-workflows)
- [LangGraph 多 agent 架构教程](https://www.analyticsvidhya.com/blog/2025/05/langgraph-tutorial-for-beginners/)
- [Agentic Design Patterns: A System-Theoretic Framework](https://arxiv.org/abs/2601.19752v1)
- [The Shift from Models to Compound AI Systems（BAIR）](https://bair.berkeley.edu/blog/2024/02/18/compound-ai-systems/)
- [Agentic AI Design Patterns（Taazaa）](https://www.taazaa.com/blog/agentic-ai-design-patterns-architecture-decisions-that-survive-production)
- [AI Agent Design Patterns 2026（aiagentrank）](https://aiagentrank.io/blog/ai-agent-design-patterns-2026)
- [2026 年多 agent 结构的收敛](https://flowhunt.io/blog/multi-agent-ai-system/)
- [Swarm 与 Agents SDK 的 handoff](https://arte.itlibra.com/en/articles/what-is-multi-agent)

## 2. 执行框架与纠偏

对应 [task-loop-evidence.md](task-loop-evidence.md)。

- [Anthropic: How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
- [How Anthropic Built a Multi-Agent Research System（ByteByteGo）](https://blog.bytebytego.com/p/how-anthropic-built-a-multi-agent)
- [Anthropic: Effective harnesses for long-running agents](https://anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [完成看产出物：harness 实践解读](https://www.mejba.me/blog/anthropic-long-running-agent-harness)
- [LangChain: Plan-and-Execute、ReWOO、LLMCompiler](https://www.langchain.com/blog/planning-agents)
- [LangGraph Pregel：超步与检查点](https://reference.langchain.com/javascript/langchain-langgraph/pregel/Pregel)
- [Magentic-One（AutoGen 文档）](https://microsoft.github.io/autogen/stable/reference/python/autogen_ext.teams.magentic_one.html)
- [Magentic 停滞干预决策](https://docs.rs/agent-framework-core/latest/agent_framework_core/workflow/enum.MagenticStallInterventionDecision.html)
- [双账本重规划实现](https://github.com/brcampidelli/chimera-agent/releases/tag/v0.3.0)
- [Cognition 多 agent 生产经验（2026-04）](https://www.zenml.io/llmops-database/multi-agent-systems-in-production-code-generation-and-review-at-scale)
- [多 agent 的 token 成本](https://www.implicator.ai/inside-claudes-research-engine-are-teams-of-ai-agents-the-future-of-artificial-intelligence/)
- [Ralph 循环（Thoughtworks Radar）](https://www.thoughtworks.com/en-de/radar/techniques/ralph-loop)
- [Ralph 反模式](https://notes.dsebastien.net/30+Areas/33+Permanent+notes/33.02+Content/Ralph+Wiggum+Technique)
- [MAST: Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657v2)
- [AFlow](https://papers.lunadong.com/paper/7283)
- [GPT-5 System Card：实时路由器](https://openai.com/index/gpt-5-system-card)

## 3. 模型评测与路由

对应 [model-evaluation.md](model-evaluation.md)。

- [Artificial Analysis：模型发布页与智能指数构成](https://artificialanalysis.ai/models/releases/gpt-6-astra)
- [Arena（原 LMArena）工作方式与 Style Control](https://botnation.ai/en/chatbot-arena/)
- [HAL: Holistic Agent Leaderboard（ICLR 2026）](https://iclr.cc/virtual/2026/poster/10006806)
- [HAL 论文要点：reasoning effort 与准确率](https://papers.lunadong.com/paper/7506)
- [Vals Index v2](https://www.vals.ai/benchmarks/vals_index_v2)
- [Epoch AI Capabilities and Benchmarking Hub 介绍](https://techjacksolutions.com/ai-brief/epoch-ai-cost-deflation-enterprise-ai-strategy-benchmarks/)
- [METR Time Horizon 说明](https://capitalandcompute.net/ai-benchmarks/metr-time-horizon/)
- [OpenRouter Rankings](https://openrouter.ai/rankings?view=daily)
- [Terminal-Bench 2.0（ICLR 2026）](https://proceedings.iclr.cc/paper_files/paper/2026/hash/444a3737adaee10d86ad2ef5f74468e6-Abstract-Conference.html)
- [基准对比：配置差异对 Terminal-Bench 的影响](https://www.buildfastwithai.com/blogs/deepswe-vs-terminal-bench-vs-osworld)
- [RouteLLM（LMSYS）](https://lmsys.org/blog/2024-07-01-routellm/)

## 4. 记忆

对应 [memory-and-retrieval.md](memory-and-retrieval.md) 第 1–4 节。

- [Mem0 Entity-Scoped Memory](https://docs.mem0.ai/platform/features/entity-scoped-memory.md)
- [Zep / Graphiti 时序知识图谱](https://signals.aktagon.com/articles/2026/07/zep-a-temporal-knowledge-graph-architecture-for-agent-memory/)
- [Letta Sleep-time agents](https://docs.letta.com/guides/agents/architectures/sleeptime/index.md)
- [LangMem SDK](https://www.langchain.com/blog/langmem-sdk-launch)
- [Claude memory tool](https://platform.claude.com/en/docs/agents-and-tools/tool-use/memory-tool)
- [ReasoningBank（Google Research）](https://research.google/blog/reasoningbank-enabling-agents-to-learn-from-experience/)
- [ACE: Agentic Context Engineering（ICLR 2026）](https://iclr.cc/virtual/2026/10018667)
- [Memory Poisoning Attack and Defense（MINJA 复现）](https://arxiv.org/abs/2601.05504v1)
- [MemGhost 报道](https://forkast.news/?p=127766)

## 5. 向量检索（RAG）

对应 [memory-and-retrieval.md](memory-and-retrieval.md) 第 5 节。

- [Hybrid RAG in 2026](https://jacar.es/en/hybrid-rag-in-2026-the-patterns-that-keep-winning/)
- [混合检索、RRF 与重排](https://www.codingshuttle.com/blogs/building-production-ready-rag-hybrid-search-reranking-and-conversational-memory)
- [Anthropic Contextual Retrieval 结果汇总](https://getcoai.com/?p=21330)
- [Qdrant 多租户：独立集合还是按字段分区](https://oneuptime.com/blog/post/2026-08-28-qdrant-one-collection-per-tenant-or-payload-partitioning/markdown)
- [多租户 RAG 隔离模式对比](https://particula.tech/blog/multi-tenant-rag-isolation-silo-pool-bridge)
- [Text Embeddings Reveal (Almost) As Much As Text](https://arxiv.org/pdf/2310.06816)

## 6. 上下文与压缩

对应 [context-engineering.md](context-engineering.md)。

- [Chroma: Context Rot](https://trychroma.com/research/context-rot)
- [Anthropic 上下文工程要点汇总](https://agentic-ai.readthedocs.io/en/latest/ContextEngineering/anthropic/)
- [上下文工程：先调召回再调精度](https://rubinlake.com/it/technology-radar/developer-ai-and-delivery/context-engineering)
- [Manus: Context Engineering for AI Agents](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- [Manus 实践解读（todo.md 记账开销）](https://tianpan.co/blog/2026-03-02-context-engineering-lessons-from-manus)
- [Claude context editing](https://platform.claude.com/en/docs/build-with-claude/context-editing)
- [Factory: Evaluating Context Compression](https://docs.factory.ai/guides/power-user/evaluating-context-compression)
- [JetBrains: The Complexity Trap](https://arxiv.org/pdf/2508.21433)

## 7. 持久化执行与可观测性

对应 [runtime-quickref.md](runtime-quickref.md)。

- [Temporal：开源持久化工作流](https://blog.elest.io/temporal-open-source-durable-workflows-for-apps-ai-agents/)
- [Temporal Replay 2026 发布内容](https://temporal.io/blog/replay-2026-product-announcements)
- [持久化执行引擎对比与 Temporal 的确定性约束](https://www.noqta.tn/blog/durable-execution-ai-agents-inngest-trigger-temporal-2026)
- [OpenTelemetry GenAI 语义约定现状](https://www.truefoundry.com/blog/opentelemetry-genai-semantic-conventions)

设计书附录 O.2 另列了 Temporal 官方限制文档、Redpanda 许可说明、Langfuse 的 OTLP 接入等选型阶段的来源。
