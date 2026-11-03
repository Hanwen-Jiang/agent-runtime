# 子 Agent 用起来：Planner 会派、派得对、合得好、算得清（2026-11-03，设计稿）

挂在哪：`feedback-2026-11-01.md` 的 **N-16 多智能体**（契约前置包 `n15-n16-contract-baseline-2026-11-02.md` 已在人工验收基线里批准），
设计书附录 E.1（Goal Node 的 sub_agent）、3.2.1 第 4 步、附录 B.3（行业研究报告的 Stage 2）、附录 F（sub_agent 用中模型）、附录 J.1（`invoke_agent` span）。
work-dag 里 N-16 在 **E 线（核心链路，单写者）**：本稿只写设计与验收清单，实现由 E 线做，或与 E 线说好后交给别的线。

> **2026-11-03 对齐**：E 线在分支 `lane/e`（`d52a497`，还没合进 main，契约 `48d029d` 待人审）已经做了 N-16 的一部分，见那边的
> `docs/plan/n16-sub-agents-implementation-2026-11-03.md`：Planner 规则 11（只给读密集、宽度型、能拆成互不重叠的几块的工作用，由下游节点合并）、
> 每个 Stage 最多 4 个 sub_agent（超了整个 Stage 拒绝）、effect 上限缺省继承 min(read, Task 上限)、sub_task 的预算压到父 Task 剩下的、
> 顶层 sub_agent 的子工作流推 `tool_status` 与 `context_usage`（`node_started` 带 `mode`）。下面第 1 节写的是 main 上的现状。
> 合并之后本稿还要做的是：第 4 节的 brief、第 5 节的来源卡片与必接的综合节点、第 6 节的预算份额与 mid 档、第 9 节的评测对照；
> 数量上限以 E 线的 4 为准（第 3 节的 8 作废，评测证明需要更多再放宽）。

## 1. 现状：执行层有，Planner 不派

| 环节 | 现在 | 问题 |
| --- | --- | --- |
| 执行 | `stage_runner._sub_agent` → 子工作流 `SubAgentWorkflow`：独立上下文、只读（effect 上限超过 read 拒绝）、跑 tool_loop、只交回结果；花费与工具调用次数加回父 Stage；取消随父工作流传播 | 可用。同一 Stage 里互不依赖的 Node 已经并行（`_slot()` 限并发） |
| Planner | 提示（`planner/prompts.py`）里**一处都没提 sub_agent**；Goal Node 的 mode 只靠模型自己想到 | 实际上从来派不出来——这是“摆设”的根因 |
| 任务说明 | 子 Agent 只拿到 Goal Node 的 goal / done_when / output_schema | 没有“范围、不做什么、交回格式、预算”的约定，研究类任务容易重复劳动、越界 |
| 合并 | 由 Planner 自己写一个综合节点（若写了）；Reviewer 有一条信号：互不依赖的 sub_agent 里有成功却交回空结果的（`reviewer/signals.py`） | 没有固定的“来源卡片”格式，综合节点要从自由文本里拼 |
| 预算 | 子 Agent 共用 Task 的预算与工具调用上限，没有每个子 Agent 的份额 | 一个子 Agent 可能把整轮的钱花光 |
| 模型 | 走 role 的默认 Tier | 设计书附录 F：只读研究的 sub_agent 用中模型；现在没有落地 |
| 评测 | task-loop 评测集里 4 道题用到 sub_agent，都在 Reviewer 一侧 | 测不出“该不该派、派几个” |
| 前端 | 进度事件只推顶层 Task | 用户看不到子 Agent 在干什么（N-16 的另一半，见第 7 节） |

## 2. 参考（2026-11-03 调研）

- **Anthropic 的多 Agent 研究系统**：主 Agent 规划、并行派子 Agent、子 Agent 只交回压缩结果；按复杂度定数量（简单查证 1 个 Agent、几次工具调用；对比 2～4 个；复杂研究 10 个以上）；
  任务说明要具体到“目标、交回格式、工具、边界”，否则重复劳动；研究类评测上比单 Agent 好约 90%，但 token 约为普通对话的 15 倍；子任务依赖多、要共享上下文的任务不适合拆。
- **OpenAI Agents SDK**：handoff（交出控制）与 agents-as-tools（调用后拿回结果，调用方保留控制）。我们的 sub_agent 是后者。
- **LangGraph**：supervisor 包已不再维护，转向 subagents 写法——主流收敛到“主 Agent + 子 Agent 当工具”。
- **MAST（失败分类）与 Google 的扩展研究**：失败集中在规格不清、协作失配、缺验证；单 Agent 已有一定水平时再加 Agent 收益小甚至为负，有的任务（例如 PlanCraft）多 Agent 一律更差。
  结论：**默认不派，有明确收益才派，并用评测证明**（与设计书 E.1 “默认不派 sub_agent”一致）。
- **A2A（Linux Foundation，v1.0）**：跨系统的 Agent 互通协议（Agent Card、任务生命周期、JSON-RPC / gRPC / REST）。管“我们与外部 Agent 互调”，不管内部分派，单列第 8 节。

## 3. 什么时候派、派几个（写进 Planner 的提示与规则）

**派 sub_agent 的条件**（同时满足 1 与 2、3 之一）：

1. 这一步是只读的：读、搜、比、查证（effect 不超过 read）。
2. 能拆成 **至少两个互不依赖** 的子问题（各自不用看别人的结果），例如“三家厂商各查一遍”“五个模块各读一遍”。
3. 或者材料太多、会挤爆主上下文（预计读入超过有效上下文的一半），需要隔离后只带回摘要。

**不派**：子问题之间要来回对齐（例如一起改同一份产物）、要写（写操作留在主流程，单写者）、一次工具调用就能答、Triage 是 fast 或 light。

**派几个**（按预计工作量，写死在规则里，Planner 只能往小调）：

| 情况 | 子 Agent 数 | 每个的工具调用上限 |
| --- | --- | --- |
| 单点查证 | 0（主流程 tool_loop） | — |
| 2～4 个对象的对比 | 2～4 | 10～15 |
| 宽度型研究（多来源、多维度） | 5～8 | 15～25 |
| 上限 | 8（Stage 并发上限 `_slot()` 照旧） | 25 |

## 4. 给子 Agent 的任务说明（固定格式）

Goal Node 的 inputs 里加一个 `brief`（JSON Schema 载荷，不进 proto），Planner 必须填，子 Agent 的首条消息由它拼出：

| 字段 | 内容 |
| --- | --- |
| objective | 一句话：要回答什么 |
| scope | 查什么、到哪为止；**不做什么**（例如“不查别的厂商”“不改文件”） |
| sources | 可选：优先的来源或路径 |
| output | 交回格式：来源卡片的数组（见第 5 节），或给定的 output_schema |
| budget | 工具调用上限、花费上限（从第 6 节分出） |

缺 `brief` 或缺 objective / scope / output 的 sub_agent，在 `planner/validate.py` 里判为结构错误（与现在的 Catalog 校验同一处），回 Planner 重写。

## 5. 合并：来源卡片 + 综合节点

- 子 Agent 默认交回 **来源卡片**：`{claim, evidence, source, confidence, open_questions}` 的数组（设计书第 3.3 节“研究、深度搜索”一行的做法），不交回全文；全文在 ObjectStore，卡片里留 obj:// 路径。
- 有两个以上 sub_agent 的 Stage，Planner 必须在后面接一个综合节点（model · transform），输入是各个子 Agent 的卡片；综合节点要列出互相矛盾的卡片与没查到的问题。
- Reviewer 现有的信号保留；加一条：卡片之间结论冲突、综合节点没有提到时，判 repair（附录 B.7 “协作失配”）。

## 6. 预算与模型

- 每个 sub_agent 的份额 = min(brief.budget, Stage 剩余预算 ÷ 本 Stage 的 sub_agent 数)。超份额时子 Agent 带着已有卡片收尾（交回部分结果并说明），不是失败。
- 子 Agent 的 tool_loop 默认用 **mid** 档（附录 F），主流程的模型不换（提示缓存）；Planner 可以为单个 sub_agent 写 tier，只能往小调。
- 父 Stage 的花费照旧加回（现有逻辑），评测与看板里另记子 Agent 的花费占比。

## 7. 观测与前端

- 每个 sub_agent 一个 `invoke_agent` span（附录 J.1，`gen_ai.agent.name` = Node id），父子 trace 关联；Langfuse 与 Tempo 里能按 Task 看到子 Agent 的树。
- 进度事件：现在只推顶层 Task。N-16 契约包已批准“新增字段旧客户端可忽略”：加 `agent_started` / `agent_finished`（Node id、objective、花费、卡片数），coding-web 右栏显示子 Agent 列表与进度。
  这一项改事件 schema（S-01 已批准的范围内），与 N-19/N-20 的事件改造一起排，不在本稿第一步。

## 8. A2A（单独的工作项，不在本稿实现）

- **对外**：Task API 前面加一层 A2A 服务端（Agent Card 放 `/.well-known/agent-card.json`，任务生命周期映射到我们的 Task 状态）；认证与租户照旧由 Ingress 定。
- **对内调用外部 Agent**：一个 `a2a_call` 工具，effect 按登记（外部 Agent 可能有副作用，默认 write_irreversible、逐次审批），凭据经 SecretStore，出网经 D-16 网关。
- 两者都改契约（S-01），依赖 N-15 的“用户自己的工具登记”。建议等第 3～6 节在评测上证明划算之后再立项。

## 9. 验收测试（先写，写完停下给人看）

| 文件 | 看什么 |
| --- | --- |
| `services/py-worker/tests/test_planner_sub_agent.py` | 提示里有第 3 节的规则；validate 拒绝缺 brief / 缺字段 / effect 超过 read / 数量超过 8 / fast、light 里派 sub_agent；两个以上 sub_agent 而没有综合节点时拒绝 |
| `services/py-worker/tests/test_sub_agent_brief.py` | 子 Agent 的首条消息由 brief 拼出；交回的卡片格式校验，不合格时带错误重问一次；超份额时带部分卡片收尾 |
| `services/py-worker/tests/test_sub_agent_budget.py` | 份额按第 6 节算；花费加回父 Stage；子 Agent 默认 mid 档，Planner 写的 tier 只能往小调 |
| `services/py-worker/tests/test_stage_replay.py` | 新增 golden：并行三个 sub_agent + 综合节点；改命令序列的部分加版本分支（`SUB_AGENT_BRIEF_PATCH`，追加到 `AT_START` 末尾） |
| `services/py-worker/tests/test_reviewer_rules.py` | 卡片冲突而综合节点没提到时判 repair |
| 评测集 `evalsets/task-loop` 新增（人审） | 约 10 道：该派（宽度型研究、多对象对比、超长材料）与不该派（单点查证、要写、子问题互相依赖）各半；分诊与形状按第 3 节标；另加 3 道 Reviewer 的卡片冲突样本 |
| 评测对照（花钱，人定预算） | 同一批研究题：不派 vs 按规则派，比质量（DeepEval 的 correctness）与花费；**派了的质量明显更好、花费在可接受的倍数内，才把规则设为默认** |

## 10. 要人定的

1. **默认打开吗**：建议先用开关（`AR_SUB_AGENT_POLICY=rules`，默认 off），第 9 节的对照通过后再默认打开。
2. **数量与花费上限**：第 3 节的表（最多 8 个、每个 25 次工具调用）与第 6 节的份额算法，是否照这个。
3. **谁来实现**：核心链路在 E 线（同事）。建议：本稿与验收测试由我们写，实现交给 E 线；或者 E 线点头后由我们做，期间 E 线不动 `stage_runner.py` 与 `planner/`。
4. **A2A**（第 8 节）是否单独立项，何时做。

## 11. 与设计书的出入（建议人改原文档）

- 附录 E.1 只写了“读密集或宽度型用 sub_agent”“默认不派”。建议补上第 3 节的条件与数量表、第 4 节的 brief、第 5 节的来源卡片与必接综合节点。
- 附录 F 写了 sub_agent 用中模型，但没写“Planner 只能往小调”；建议补上。
