# 模式参考：Agent 模式的叫法、出处、流程与落点

> 来源：`模式图谱`，以及 `主文档` 第一节第 5 小节；与[设计书](../design/agent-runtime-design.md)附录 N 对应。
> 已按设计书附录 A 改用新术语。原文的流程图没有随 md 导出，本文改用文字流程；原图见 `Z:\agentruntime\模式图谱.docx` 或 `.pdf`。
> 用途：实现 Stage 形状、Goal Node 的 mode、quality、reasoning 时，查每个模式的标准做法和出处。

## 1. 结论先行

本设计不另立一套模式分层。所有模式都是 Task Loop 里的**配置**，分别落在以下六个位置：

| 组 | 决定什么 | 在本设计中的落点 |
| --- | --- | --- |
| A 推理策略 | 一次模型调用内部怎么想 | 生成型 model 节点的 `reasoning` |
| B 骨架 | 下一步由谁、在什么时候决定 | Stage 的形状；Goal Node 的 `mode` |
| C 质量包装 | 用更多算力换质量 | 节点或子图的 `quality` |
| D 执行方式 | 几个 agent、谁掌控流程、谁能写 | `mode`（sub_agent、sub_task）；Verdict；Model Gateway 升级 |
| E 头尾状态 | 让任务跨上下文窗口、跨 Session 持续推进 | Task State、Memory、Recipe |
| F 选型、闸门与离线优化 | 用哪个做法、哪里要人把关、离线怎么改进 | Triage、Model Gateway、gate 节点、Learning Pipeline |

三条贯穿约束：

- **写操作单一写者**：sub_agent、审查者、更强的模型只贡献判断，不直接改共享产物。
- **评估者的可靠程度决定 C 组值不值得做**：有硬判据时收益最大；纯自评常常只改措辞。
- **自动搜索依赖评测集**：评测集覆盖不到的任务，搜出来的做法不一定更好。

## 2. 业界怎么分、怎么叫

### 2.1 主要分类法

业界没有统一的分层，常见清单从 4 个到 21 个模式不等，多数只区分单 agent 与多 agent。

| 来源 | 怎么分 | 数量 |
| --- | --- | --- |
| [Andrew Ng（DeepLearning.AI，2024）](https://www.augmentcode.com/guides/agentic-design-patterns) | 反思、工具使用、规划、多 agent 协作 | 4 |
| [Anthropic《Building Effective Agents》（2024-12）](https://simonwillison.net/2024/Dec/20/building-effective-agents) | 先分 workflow（预定义代码路径）与 agent（LLM 自主决定流程）；workflow 再分提示链、路由、并行、编排者-工人、评估者-优化者 | 5 种 workflow + agent |
| [Google ADK 多 agent 模式指南（2026-01）](https://infoq.com/news/2026/01/multi-agent-design-patterns) | 用顺序、循环、并行三种原语组合出：顺序流水线、协调者/分派者、并行扇出/汇聚、分层任务分解、生成者与评审者、迭代精炼、人在环、组合模式 | 8 |
| [Antonio Gullí《Agentic Design Patterns》（Springer）](https://www.ebooks.com/en-ai/book/347123043/agentic-design-patterns/antonio-gull/) | 每章一个模式，从提示链、路由到记忆、工具、多 agent、护栏、评估 | 21 |
| [LangChain / LangGraph](https://www.langchain.com/blog/langgraph-multi-agent-workflows) | “认知架构”指单个 agent 的控制流程；多 agent 分为网络、Supervisor、分层，配合 handoff（[来源](https://www.analyticsvidhya.com/blog/2025/05/langgraph-tutorial-for-beginners/)） | — |
| [系统论框架（arXiv，2026-01）](https://arxiv.org/abs/2601.19752v1) | 基础、认知与决策、执行与交互、自适应与学习四类 | 12 |
| 2026 年实践文章 | 例如 ReAct、反思、计划-执行、工具使用、多 agent 编排五类（[来源](https://www.taazaa.com/blog/agentic-ai-design-patterns-architecture-decisions-that-survive-production)），也有九类的版本（[来源](https://aiagentrank.io/blog/ai-agent-design-patterns-2026)） | 5 到 20 |

### 2.2 都叫什么

| 叫法 | 谁在用 | 指什么 |
| --- | --- | --- |
| Agentic design patterns | Andrew Ng、Gullí、Google | 可复用的结构模板 |
| Agentic workflow | Andrew Ng、Anthropic | Anthropic 用来特指“按预定义代码路径编排 LLM 与工具”，与自主 agent 相对 |
| Agentic system | Anthropic | workflow 与 agent 的总称 |
| Cognitive architecture | LangChain | 一个 agent 的控制流程与状态机 |
| Multi-agent architecture / topology | LangGraph、学术界 | 多个 agent 之间的结构：网络、Supervisor、分层，或链、星、网 |
| Orchestration pattern | Google ADK 等框架 | 用顺序、并行、循环等原语组合 agent |
| [Compound AI system](https://bair.berkeley.edu/blog/2024/02/18/compound-ai-systems/) | Berkeley BAIR（2024-02） | 由多次模型调用、检索器、工具等组件组成的系统，范围最宽 |
| Scaffold、harness | [HAL](https://iclr.cc/virtual/2026/poster/10006806)、Anthropic | 包住模型、实际执行流程的程序；评测中常与模型分开比较 |

### 2.3 本项目的用词

| 词 | 在本项目里指什么 |
| --- | --- |
| 模式（pattern） | 可复用的模板，即本文列出的这些 |
| Recipe | 某个任务族验证过的做法：Stage 划分、模板、Tier |
| Task Plan | 为一个 Task 组装出的具体配置，由 Triage 产出（附录 C.3） |
| Runtime | 执行这些配置的运行时，即业界说的 harness |

对外沟通时，Recipe 或 Task Plan 可以译为 agentic workflow 或 agent architecture。

## 3. 模式目录

每行依次给出：模式、专业名称与出处、流程、在本设计中的落点。

### 3.1 A 推理策略 → `reasoning`

这一组只决定一次模型调用内部怎么想，不涉及工具和外部循环。

| 模式 | 专业名称与出处 | 流程 | 落点 |
| --- | --- | --- | --- |
| 逐步推理 | Chain-of-Thought Prompting（[Wei et al., NeurIPS 2022](https://arxiv.org/abs/2201.11903)）；Zero-shot CoT（[Kojima et al., 2022](https://arxiv.org/abs/2205.11916)） | 问题 → 在输出里逐步推理 → 答案 | `reasoning = stepwise`，用于需要把推理过程留在输出里审计的场景 |
| 原生思考 | Extended thinking、reasoning models（各模型厂商的思考预算或 reasoning effort 参数） | 问题 → 模型内部思考（预算受控）→ 答案 | `reasoning = direct`；思考预算按 Tier 设，推理模型默认用它 |
| 先计划后求解 | Plan-and-Solve (PS / PS+) Prompting（[Wang et al., ACL 2023](https://arxiv.org/abs/2305.04091)）；相近：Least-to-Most（[Zhou et al., 2022](https://arxiv.org/abs/2205.10625)） | 理解题意、列出变量 → 制定计划 → 按计划逐步求解 → 答案 | `reasoning = plan_first`，用于小模型或非推理模型，减少漏步骤 |
| 自组推理结构 | SELF-DISCOVER（[Zhou et al., 2024](https://arxiv.org/abs/2402.03620)） | 阶段 1（每个任务族一次）：从推理模块库选择 → 改写 → 组成结构化推理计划；阶段 2（每个实例）：按结构逐项填写 → 答案 | `reasoning = templated`；阶段 1 的产物就是 Reasoning Template，由 Learning Pipeline 生成并经 Registry 发布 |

Self-Discover 与 Plan-and-Solve 影响推理的“结构”，CoT 与原生思考影响逐步推理的“深度”。默认选择见设计书附录 E.4：推理模型默认 direct，推理强度要按任务族实测。

### 3.2 B 骨架 → Stage 形状与 Goal Node 的 `mode`

这一组决定“下一步由谁、在什么时候决定”：由代码决定的是 workflow，由 LLM 每步决定的是 agent，一次决定多步的是规划类模式。

| 模式 | 专业名称与出处 | 流程 | 落点 |
| --- | --- | --- | --- |
| 固定链 + 门控 | Prompt Chaining（[Anthropic, 2024](https://lyzr.ai/?p=56328)）；Sequential Pipeline（Google ADK） | 步骤 1 → 程序检查 → 步骤 2 → … | 串行依赖的 Atomic Node；检查用 program 执行的 judge 或 gate |
| 路由 | Routing（[Anthropic, 2024](https://lyzr.ai/?p=56328)）；Coordinator / Dispatcher（Google ADK） | 输入 → 分类 → 分派到一个分支 | `role = route` 的节点，输出只能是枚举；Triage 本身也是路由 |
| ReAct | ReAct（[Yao et al., ICLR 2023](https://arxiv.org/abs/2210.03629)）；Tool-calling agent、agent loop | 思考 → 调工具 → 观察 → 循环直到完成 | Goal Node `mode = tool_loop`；每一步是一个 activity，设最大迭代次数 |
| 计划-执行 | Plan-and-Execute（[LangChain, 2024](https://www.langchain.com/blog/planning-agents)） | 规划全部步骤 → 逐步执行 → 必要时重规划 | 信息齐全时，Planner 一轮发出完整 DAG；Reviewer 判 replan |
| ReWOO | ReWOO（[Xu et al., 2023](https://arxiv.org/abs/2305.18323)）；Planner–Worker–Solver | 一次写出带变量占位的完整计划 → 执行各步并填入变量 → Solver 汇总；中间不再调用 LLM | 节点 inputs 引用上游 result 字段；aggregate 或 deliver 节点担任 Solver |
| LLMCompiler | An LLM Compiler for Parallel Function Calling（[Kim et al., ICML 2024](https://arxiv.org/abs/2312.04511)） | 规划器输出任务 DAG → 按依赖并发调度 → 汇合 | Stage 内无依赖的节点并发执行，扇出设上限 |
| 编排者-工人 | Orchestrator-Workers（[Anthropic, 2024](https://lyzr.ai/?p=56328)）；Hierarchical Task Decomposition（Google ADK） | 编排者看结果后动态生成下一批子任务 → 工人执行 → 回到编排者 | 部分未知时每轮一个 Stage；Planner 看了结果再规划下一轮 |
| CodeAct | CodeAct（[Wang et al., ICML 2024](https://arxiv.org/abs/2402.01030)） | 用可执行代码作为动作 → 执行 → 观察结果 | tool_loop 中的动作由 program 节点在 Sandbox 执行 |

ReWOO 与 LLMCompiler 都是计划-执行的变体：前者省掉中间的 LLM 调用，后者把计划编译成可并行的 DAG。编排者-工人与 LLMCompiler 形状相近，区别在于子任务是运行中动态生成的。

步长怎么选，见设计书 3.2.1：信息齐全时一轮发完整 DAG，部分未知时每轮一个阶段，几乎未知时只发一个 Goal Node 去探索。

### 3.3 C 质量包装 → `quality`

这一组全部是“生成候选 → 评估 → 选择、聚合或修订”的变体，差别在于候选怎么来（迭代、并行、树形）以及评估者是谁。

| 模式 | 专业名称与出处 | 流程 | 落点 |
| --- | --- | --- | --- |
| 生成 → 评估 → 修订 | Self-Refine（[Madaan et al., NeurIPS 2023](https://arxiv.org/abs/2303.17651)）；Reflexion（[Shinn et al., NeurIPS 2023](https://arxiv.org/abs/2303.11366)）；CRITIC（[Gou et al., 2023](https://arxiv.org/abs/2305.11738)）；Evaluator-Optimizer（Anthropic, 2024） | 生成 → judge → 不通过就带反馈修订 → 最多 N 轮 | `quality = revise`；judge 优先用 program（测试、规则），其次用另一家模型按 rubric 评审。Reflexion 的反思对应 failure 类记忆 |
| 并行采样 → 聚合 | Self-Consistency（[Wang et al., ICLR 2023](https://arxiv.org/abs/2203.11171)）；Best-of-N + LLM-as-a-Judge（[Zheng et al., 2023](https://arxiv.org/abs/2306.05685)）；Multiagent Debate（[Du et al., ICML 2024](https://arxiv.org/abs/2305.14325)）；Mixture-of-Agents（[Wang et al., 2024](https://arxiv.org/abs/2406.04692)） | 并行生成 N 个 → 投票（答案可归一时）或 judge 择优；辩论是多轮互评；MoA 是多模型分层聚合 | `quality = ensemble`；judge 用两两比较并交换顺序，防位置偏差（附录 B.7） |
| 树与图搜索 | Tree of Thoughts（[Yao et al., NeurIPS 2023](https://arxiv.org/abs/2305.10601)）；LATS（[Zhou et al., ICML 2024](https://arxiv.org/abs/2310.04406)）；Graph of Thoughts（[Besta et al., AAAI 2024](https://arxiv.org/abs/2308.09687)） | 每层扩展 k 个候选 → 打分 → 剪枝 → 可回溯；LATS 用蒙特卡洛树搜索并结合环境反馈 | `quality = search`，只用于有死胡同的组合问题，成本最高 |

三族可以叠加：例如每个树节点内部再做 Best-of-N，或在修订循环里用投票当评估器。quality 可以挂在单个 Node 上，也可以挂在整个子图上（附录 E.2）。

### 3.4 D 执行方式 → `mode`、Verdict 与升级

这一组决定几个 agent、谁掌控流程、上下文是否共享、谁能写。单 agent 是默认，多 agent 只在读密集或需要独立视角时才值得。

| 模式 | 专业名称与出处 | 流程 | 落点 |
| --- | --- | --- | --- |
| 单 agent + 只读子代理 | Subagents；Agents-as-tools（Claude Code 子代理；[OpenAI Agents SDK](https://jacar.es/en/google-agent-development-kit/)） | 主 agent 派出子代理 → 子代理在独立上下文中工作 → 只交回摘要 | Goal Node `mode = sub_agent`，默认只读 |
| Supervisor | Supervisor architecture；Hierarchical Agent Teams（[LangGraph, 2024](https://www.langchain.com/blog/langgraph-multi-agent-workflows)） | 中心 agent 分派 → 下属执行 → 回报中心 | Planner 担任中心；下属用 sub_agent 或 sub_task，嵌套不超过 2 层 |
| Handoff | Handoffs（OpenAI Swarm 2024；[Agents SDK 2025](https://arte.itlibra.com/en/articles/what-is-multi-agent)） | 当前 agent 判断不归自己管 → 把会话交给另一个 agent | `Verdict = reroute`：换 Recipe 继续 |
| 生成者 + 审查者 | Generator–Critic；Maker–Checker（[Google ADK](https://developers.googleblog.com/en/developers-guide-to-multi-agent-patterns-in-adk/)；金融业“四眼原则”） | 生成者产出 → 审查者独立审查 → 退回或通过 | `quality = revise`，judge 用另一家模型；或一个 `role = judge` 的节点 |
| 级联升级 | LLM Cascade（[FrugalGPT, Chen et al., 2023](https://arxiv.org/abs/2305.05176)）；Cognition 称为 smart friend（[2026](https://www.zenml.io/llmops-database/multi-agent-systems-in-production-code-generation-and-review-at-scale)） | 先用便宜模型 → 置信不足时升级到更强模型 | Model Gateway 的升级：先调推理强度，再换模型；`Verdict = escalate` |
| 黑板 | Blackboard architecture（经典 AI 架构；LLM 版：[Han & Zhang, 2025](https://arxiv.org/abs/2507.01701)、[Salemi et al., 2025](https://arxiv.org/abs/2510.01285v2)） | 多个参与者读写同一个共享状态 | Task State 作为共享状态 |
| Map-Reduce | Parallel Fan-Out / Gather；Sectioning；Map-Reduce（Google ADK、Anthropic、LangGraph） | 拆分 → 并行处理 → 汇合 | Stage 内并行节点 + `role = aggregate`；大批量用 template 模式按项展开 |

“写操作单一写者”是这一组最重要的约束：子代理、审查者、更强的模型都只贡献判断，不直接改共享产物（设计书 3.2.3）。

### 3.5 E 头尾状态 → Task State、Memory、Recipe

这一组解决“上下文会丢”的问题：把进度、笔记、记忆和技能放在上下文之外，让任务能跨多个上下文窗口、多个 Session，甚至多个 Task 持续推进。

| 模式 | 专业名称与出处 | 流程 | 落点 |
| --- | --- | --- | --- |
| 研究循环 | Iterative retrieval；Self-Ask（[Press et al., 2022](https://arxiv.org/abs/2210.03350)）；IRCoT（[Trivedi et al., ACL 2023](https://arxiv.org/abs/2212.10509)）；也叫 Agentic RAG | 提出子问题 → 检索 → 推理 → 再提子问题 → 直到能回答 | tool_loop 中的检索循环 |
| 深度研究 | Deep Research agents；STORM（[Shao et al., NAACL 2024](https://arxiv.org/abs/2402.14207)） | 澄清范围 → 多路并行研究 → 综合成文 → 引用核验 | 附录 B.3 的“行业研究报告”：gate → 3 到 5 个 sub_agent → 综合、核验与 deliver |
| 长时程 harness | Harness for long-running agents（[Anthropic, 2025-11](https://anthropic.com/engineering/effective-harnesses-for-long-running-agents)） | 初始化阶段写功能清单与进度文件 → 每个会话做一个功能并验证 → 提交并更新进度 | 跨 Session 的 Task State 加 Round 循环：Planner 读功能清单与进度，Reviewer 提交并更新进度 |
| Ralph 循环 | Ralph Wiggum loop（Geoffrey Huntley, 2025；[Thoughtworks Radar 2026-04](https://www.thoughtworks.com/en-de/radar/techniques/ralph-loop)） | 固定提示与规格 → 新上下文执行一轮 → 外部判据检查 → 重复 | 同上；必须设最大迭代次数，并检测重复出现的同一错误（附录 B.8） |
| 技能库 | Skill library（[Voyager, Wang et al., 2023](https://arxiv.org/abs/2305.16291)）；Agent Skills（Anthropic, 2025）；Agent Workflow Memory（[Wang et al., 2024](https://arxiv.org/abs/2409.07429)） | 成功的做法沉淀为可复用技能 → 下次直接调用 | Recipe 与 Node Catalog；高频 Goal Node 固化为 template 模式 |

长时程 harness 与 Ralph 的共同点是每轮用新上下文，并依赖外部判据；区别在于 harness 有初始化阶段和结构化的进度文件，Ralph 只靠固定提示与规格。

### 3.6 F 选型、闸门与离线优化 → Triage、Model Gateway、gate、Learning Pipeline

这一组不在单个 Stage 里面，而是决定用哪个做法、在哪里要人把关，并在离线把做法改得更好。三者构成闭环：路由选做法，闸门守住高风险动作，trace 与评测驱动离线搜索，再回写路由规则。

| 模式 | 专业名称与出处 | 流程 | 落点 |
| --- | --- | --- | --- |
| 投入分级与路由 | Effort scaling（[Anthropic, 2025](https://www.anthropic.com/engineering/multi-agent-research-system)）；LLM routing（[RouteLLM, Ong et al., 2024](https://arxiv.org/abs/2406.18665)）；compute-optimal test-time scaling（[Snell et al., 2024](https://arxiv.org/abs/2408.03314)） | 估计难度与价值 → 分配模型与算力 | Triage（fast、light、full、replan）与 Model Gateway 的选档 |
| 人在环 | Human-in-the-loop；interrupt；approval gate（[Google ADK](https://infoq.com/news/2026/01/multi-agent-design-patterns)；LangGraph interrupt） | 到达关键动作前暂停 → 人审批或补充 → 继续 | gate 节点 + HumanChannel；write_irreversible 前由 Hook 自动插入 |
| 自动做法搜索 | ADAS / Meta Agent Search（[Hu, Lu & Clune, ICLR 2025](https://arxiv.org/abs/2408.08435)）；AFlow（[Zhang et al., ICLR 2025](https://arxiv.org/abs/2410.10762)）；GPTSwarm（[Zhuge et al., ICML 2024](https://arxiv.org/abs/2402.16823)） | 在评测集上搜索或演化更好的流程 → 评测 → 保留更优者 | Learning Pipeline 的离线 Recipe 搜索，结果走影子评测后经 Registry 发布 |

## 4. 与设计书的对照

| 要查什么 | 看设计书哪里 |
| --- | --- |
| 每个模式落在哪个字段（总表） | 附录 N |
| Goal Node 的 mode、quality、reasoning 怎么展开 | 3.2.3、附录 E |
| Stage 形状由哪些任务维度决定 | 附录 C.5（11 个维度与“可验证性 × 耦合度”四象限） |
| 形状不对的症状与调整 | 附录 B.7 |
| 六类常见任务的 Stage 划分示例 | 附录 B.3 |
