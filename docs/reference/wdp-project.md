# WDP 项目（StoreAgent）：总结与可借鉴之处

> 来源：一段 Codex 对话的分享页（[链接](https://chatgpt.com/s/cx_6adc9e036a388191a3ea7328e05aa50b)，2026-10-13 至 10-22），内容是 Codex 读 StoreAgent 仓库后整理的技术栈、搭建过程与三张流程图。本文记为“WDP 项目”。
> 我们没有读过 WDP 的源码。下面的事实都转述自那段对话；对话里 Codex 自己也说过：Git 历史读不到，搭建顺序是按依赖关系重建的，示例流程是按评测语料推演的，不是线上 trace。
> 本项目一侧的现状核对于 2026-10-24（main，7613d04）。术语按设计书附录 A；WDP 自己的对象名（Thread、Run、Event、Checkpoint 等）保留原名，对照见第 3 节。

## 1. 一句话

WDP 是一个以知识库为核心的单体 Agent 应用（前端叫“知识助手”）：文档版本化入库 → 混合检索 → LangGraph 取证流程 → LangChain 外层 Agent 生成回答；执行过程以 Run、Event、Checkpoint 持久化在 Postgres，前端经 SSE 按序号补放。

它和我们的关系：WDP 在“检索取证”这条链上做得比我们细，在“持久执行、多租户、治理”上比我们弱。值得借鉴的主要是前者。

## 2. 技术栈与进程

| 层 | 技术 | 作用 |
| --- | --- | --- |
| 前端 | Vue 3.5、TypeScript 5.9、Pinia、Axios、Vite 8、marked、Highlight.js、DOMPurify | 聊天、知识库、模型管理、工具管理、评测工作台；SSE 事件经 Reducer 更新 Pinia |
| 后端 | Python 3.12+、FastAPI、Uvicorn、Pydantic 2 | HTTP 与 SSE；lifespan 管理资源，启动时只检查 schema 版本，不自动迁移 |
| Agent 与流程 | LangChain（`create_agent`、middleware）、LangGraph（State、条件边、Send 并行） | 外层 Agent 调工具；知识库工具内部是一张 LangGraph 取证图 |
| 数据访问 | SQLAlchemy 2、Alembic、psycopg | 业务库与迁移（迁移 0001–0018） |
| 知识处理 | BGE-M3（sentence-transformers）、PyPDF、docx2txt、Unstructured、openpyxl | 稠密向量与文档解析 |
| 存储 | PostgreSQL 15、Redis 7（Streams）、Milvus 2.5（HNSW + 原生 BM25） | Postgres 是事实来源；Redis 做通知、父块缓存、限流；Milvus 做稠密与 BM25 检索 |
| 工具与安全 | JWT、RBAC、Guardrail、Docker Sandbox、SQLGlot、Tavily Extract | 认证、权限、工具调用前检查、代码隔离、SQL 语法分析、网页抽取 |
| 工程 | uv、npm、pytest、Vitest、Playwright、Ruff、mypy、ESLint；JSON Schema 生成 Python 与 TS 类型 | 测试与类型；前后端共享的事件与工具结果格式由 schema 生成 |

常驻进程三个：API（HTTP、SSE、Run 执行器、前端静态资源同源托管）、Indexing Worker（文档构建、发布、清理）、Evaluation Worker（评测）。没有单独的 Agent Worker，Run 在 API 进程里执行。Docker Compose 只拉基础设施（Postgres、Redis、Milvus 及其 etcd、MinIO、Attu），应用进程另行启动。

## 3. 核心对象与对照

| WDP 对象 | 含义 | 本项目里最接近的 |
| --- | --- | --- |
| Thread | 一段对话（迁移 0013 由 Session 统一改名） | Session |
| Run | 一次完整的 Agent 执行；提交时预留用户消息与助手消息，带幂等键 | Task（`client_request_id` 去重） |
| Message | 对话中的一条消息；回答增量写进预留的助手消息 | Session 的追加记录；deliver 节点的流式输出 |
| Event | Run 内有序的事实，带 sequence | TaskEvent（SSE，`event_id` 由业务库分配） |
| Checkpoint（RunCheckpoint） | 暂停等用户补充时保存的取证状态 | Temporal 事件历史；human 节点挂起等信号 |
| Model Profile、Assignment、Snapshot | 模型配置、角色分配、Run 创建时冻结的快照 | Model Profile；Task 提交时固定的 Registry 修订号（3.1.2） |
| Answer、Fast、Grader、Evaluator | 四种模型角色 | role 与角色默认 Tier（附录 H.3） |
| Document、Document Version、Index Job | 文档的稳定身份、版本、入库任务 | 设计书 I.6 的“Workspace 知识”，尚无实现 |
| Registry、Skill、Guardrail、Sandbox | 工具定义、指令与工具组合、调用前检查、代码隔离 | Registry（Node Template、Recipe）、Policy Engine 的 Hook、ToolDriver、Sandbox |

## 4. 主链路

### 4.1 文档入库：先建新版本，校验通过再切指针

```text
上传文件 → API 保存文件，创建 Document Version 与 Index Job，立即返回
→ Indexing Worker 领取任务（租约、心跳、重试、fencing 防止旧 Worker 迟到写入）
→ 解析 → 清洗 → 三级父子分块 → L1、L2 父块写 Postgres；L3 叶子块嵌入后写 Milvus
→ 校验片段 ID、数量与 Manifest
→ 在 Postgres 里原子切换 Document.current_version
```

- 三级分块按字符计：L1 2,400（重叠 400）、L2 1,600（重叠 200）、L3 800（重叠 100）；切分点优先落在段落、中文标点、换行上。
- 每个片段带 tenant、知识库、document、document version、页码、层级、父块与根块、内容哈希。权限过滤、引用展示、版本替换、父块扩展都靠这些身份。
- 新版本构建失败时旧版本照常可用。“原子”只指 Postgres 里版本指针的切换，Postgres 与 Milvus 之间没有跨库事务。
- 不用 FastAPI 的 BackgroundTasks 做入库，因为它不持久、不可重试。

### 4.2 检索：混合召回 → 去重 → 父块扩展 → 可选 Rerank

- 稠密：BGE-M3，查询与文档分开编码；Milvus HNSW，内积距离。
- 稀疏：Milvus 原生 BM25 Function（不是 BGE-M3 的稀疏输出），补型号、错误码、字段名这类精确匹配。
- 两路用 RRF 融合。
- 父块扩展（Auto-merging）：同一个 L2 下至少命中 2 个 L3 时用 L2 替换碎片，必要时再升到 L1；必须核对 tenant、文档与版本一致，不能从别的版本借父块。
- Rerank 可选；失败时保留召回顺序，但要记录失败与降级，不能装作精排过。

### 4.3 取证流程：LangGraph 图，只取证、不作答

```text
判断问题复杂度
→ 简单问题：检索 → 证据评判
    ├ 证据足够 → 返回证据
    ├ 表达需要改善 → 改写一次（Step-back 或 HyDE）→ 再检索 → 再评判
    ├ 缺少必要条件 → 请求澄清（保存 Checkpoint，Run 进入 waiting_input）
    └ 检索正常但没有依据 → 返回“证据不足”
→ 多事实问题：拆成 2–4 个子问题，Send 并行检索与评判 → 证据聚合节点去重、检查覆盖缺口
```

- 评判看的是“能否支持回答”，不只是“主题相关”。
- HyDE 生成的假设文本只用于检索，不能当证据进答案。
- 改写次数有上限，避免无界成本。
- 本次 Run 能检索的文档版本范围在取证开始时固定。
- 状态对象 `RAGState` 携带问题、检索结果、路由、改写次数、澄清信息、快照与 trace。

### 4.4 问答：外层 Agent 调工具，最后由 Answer 模型作答

```text
创建或复用 Run（预留消息、冻结模型快照）→ RunAgentExecutor 领取
→ 准备执行数据（历史、快照、用户与租户、超时）
→ AgentRuntimeFactory.create：create_agent 装配模型、工具、提示词、middleware
→ runtime.astream 启动
   → middleware：组织上下文、控制长度、限制工具与调用次数、循环检测
   → Answer 模型返回工具调用 → 后端检查权限、策略与预算 → 执行 search_knowledge_base（内部跑 4.3 的图）
   → 结果作为 ToolMessage 追加 → 再调 Answer 模型 → 返回文本，结束
→ Run 执行器判断：正常完成，或已有澄清暂停
```

### 4.5 持久执行：Event 先落库，Redis 只做通知

```text
Run 状态变化 → 同一事务写 Event + Outbox
→ Outbox 发布到 Redis Streams（通知）
→ SSE 从 Postgres 的 Event Journal 读出并推送
→ 前端 Reducer 按 sequence 去重、补缺、更新 Pinia
```

前端约定：重复事件不能重复追加文本；缺事件要补放；切换 Thread 后旧请求不能污染新页面；SSE 断开不等于 Run 结束；点“停止”要等后端确认取消。

进程崩溃后，失去执行所有权的 Run 被终结为 orphan，不承诺从生成中途续跑。

### 4.6 澄清与恢复

```text
waiting_input → 用户补充 → POST /v1/runs/{run_id}/resume
→ 恢复同一个 Run：校验权限与恢复凭据，读 Checkpoint
→ resume_rag_state：把补充并入原问题，定向检索并重新评判
   ├ 仍需澄清 → 保存新的暂停状态，继续等
   ├ 有证据 → 用原 Run 的模型快照建一个关掉工具的 Agent，只负责作答
   └ 无证据且不需澄清 → 生成说明
→ 保存最终消息，完成同一个 Run
```

正式路径用项目自己的 RunCheckpoint，不是 LangGraph 原生的 PostgresSaver。

### 4.7 工具与安全

Registry 定义能力 → Skill 组织指令与工具 → 当前 Run 只看到允许的工具 → Guardrail 在执行前判定本次调用 → 执行或进 Sandbox → 记录结果与审计。几条原则：Sandbox 不替代授权；工具没出现在模型上下文里，也不替代服务端执行检查；SQL 工具用 SQLGlot 做只读与查询限制；Web Research 在 Run 内给来源编 S1、S2 等编号（ADR0026）。认证上 Access Token 只放浏览器内存，Refresh Token 走 HttpOnly Cookie，处理轮换、重放、退出与多标签页并发。

### 4.8 模型治理与评测

- Provider Runtime：一次调用只有一个重试所有者；deadline 覆盖排队、调用与重试等待；失败返回明确的错误类型；不把网络失败解释成“知识库没有答案”；Embedding 延迟加载并限并发。
- 评测：版本化 Dataset → 创建 Evaluation Job 并冻结配置 → Evaluation Worker 逐条执行 → 收集检索结果、答案、错误、耗时 → 检索段算 Recall、MRR、NDCG，答案段评正确性、依据性、完整性 → 与可比较的 baseline 对照，过质量门禁。
- 离线的 `contract_smoke` 只验证评测契约与评分能否重建，不代表线上准确率。

### 4.9 演进记录（迁移与 ADR）

| 记录 | 变化 |
| --- | --- |
| 0001 | 用户、对话、消息、父块（旧结构基线，不是最初版本） |
| 0002–0006 | Run、Message、Event、Checkpoint、幂等、执行所有权 |
| 0007–0008 | 文档版本发布、Indexing Worker、任务租约与清理 |
| 0009–0010 | Skill、Tool 注册，Guardrail、Sandbox 状态 |
| 0011–0013 | 认证记录；移除旧文档结构；Session 统一为 Thread |
| 0014–0017 | 模型配置、Run 模型快照、持久化评测 |
| 0018 | 能力管理：管理员在后台配置 Skill、HTTP Tool、SQL |
| ADR0026 | Web Research 改为 Run 内的 Source ID 与 Tavily Extract 协议 |

## 5. 对照

| 维度 | WDP | 本项目 | 谁更强 |
| --- | --- | --- | --- |
| 持久执行 | 自建 Run、Event、Checkpoint；租约、心跳、fencing；崩溃后 orphan，不续跑 | Temporal：重放、重试、信号、版本分支；Worker 崩溃后接着跑（S-07 故障注入） | 本项目 |
| 编排 | LangChain 外层 Agent + LangGraph 取证图 | 自研 Task Loop（Planner → Stage → Reviewer），八种 Verdict，Triage 四级 | 本项目更通用；WDP 在“问答”这一个场景里更细 |
| 文档入库 | 版本化、Index Job、三级分块、原子发布 | 设计书 I.6 列了“Workspace 知识”，计划没有工作项，代码没有 | WDP |
| 检索 | 稠密 + BM25 + RRF → 父块扩展 → 模型 Rerank | 结构化预过滤 → 错误签名精确匹配 → 稠密 + 稀疏 + RRF → 启发式重排（查询词覆盖率 + 稠密相似度）→ 信任 × 新鲜度 × 作用域 → MMR | 召回后处理 WDP 细；过滤、打分与治理本项目细 |
| 取证判定 | 证据评判 + 受限改写 + 澄清 + 多事实拆分与覆盖检查 | Reviewer 按验收条件判 Task；检索本身单次、无充分性判定；Planner 还没接 Memory Service（`planner/context.py` 用内存实现） | WDP |
| 事件与 SSE | Event 与状态同事务落 Postgres，Outbox → Redis Streams 通知，SSE 读库；有回答增量与工具事件 | `event_id` 由业务库分配，事件正文只在 Cache；Cache 丢了只能补发 task_finished；还没有思考、工具状态与 token 增量 | WDP |
| 澄清 | RunCheckpoint + resume 接口，恢复同一个 Run | human 节点挂起，回复以信号送回工作流；`human_input_required` 带 question_id | 本项目（不需要自建 Checkpoint） |
| 模型治理 | 四个角色、Profile、Run 快照、单一重试所有者 | Model Gateway：Tier、期望成本选档、限流、换家、计费；Registry 固定修订号；judge 用不同家 | 本项目；重试所有权这一条 WDP 写得更明确 |
| 工具安全 | Registry、Skill、Guardrail、Sandbox、SQLGlot | Registry 模板核对、Policy Engine、未登记工具按 write_irreversible 拒绝、幂等键与 gate | 相当；本项目对写操作更严 |
| 多租户 | 片段带 tenant 字段 | RLS、按租户密钥加密、Qdrant 强制过滤、越权测试进 CI | 本项目 |
| 评测 | 检索段与答案段分开打分；Dataset 版本化；baseline 与门禁 | Task 级评测集，开发集与留出集，`--repeat` 中位数，非劣效门槛；检索段指标（设计书 I.8）未实现 | 各有所长 |
| 前端 | Vue 全套，SSE Reducer | 无前端 | — |

## 6. 值得借鉴

按优先级排。每条写清 WDP 怎么做、我们现在怎样、建议怎么落、挂在哪个工作项、是否要改设计书或契约。

### 6.1 第一档：补的是我们缺的能力

**① Workspace 知识入库：版本化文档与原子发布**

- WDP：Document 是稳定身份，Document Version 是一次构建；新版本校验 Manifest 后才切 `current_version`，失败不影响旧版本。
- 我们：设计书 I.6 已列“Workspace 知识：文档、工单、wiki”（片段 + 上下文前缀，权限继承源系统），但计划第 7 节没有对应工作项，Memory Service 只管 Memory Item。
- 建议：照搬“先建后切”的发布模型，片段身份带 tenant、workspace、document、version、层级、父块、内容哈希。入库任务做成 Temporal 工作流，租约、心跳、fencing 都由 activity heartbeat 与 Workflow ID 唯一性提供，不必照抄 WDP 的租约表。指针切换在 RelationalStore 一个事务里做，与 VectorStore 之间同样不追求跨库原子：查询时按 `current_version` 过滤即可挡住半成品。
- 挂在：计划第 7 节“Memory Service 检索流水线”（阶段 5）。新增的 RPC 与载荷要走 S-01 流程。设计书 I.6 需补“版本与发布”一段（见第 8 节）。

**② 事件正文落业务库，Cache 只做通知**

- WDP：Event 与状态变化同事务写 Postgres（附 Outbox），Redis Streams 只负责叫醒 SSE，SSE 从库里读。
- 我们：`internal/events/events.go` 的事件正文只在 Cache 里；Cache 丢失时中间事件找不回，只能补发 task_finished。思考、工具状态与 token 增量还没做（R-05 遗留）。
- 建议：`event_id` 已经由 taskstore 分配，再多写一行正文的成本很小。SSE 续传先读库，Cache 只做低延迟通知与多副本广播。token 增量这类高频小事件可以不落库，只把节点级事件落库，在事件类型上区分“可丢”与“必达”。
- 挂在：R-05 的后续，计划第 7 节“Task Service API 与 Ingress”。设计书 4.5、4.14 要改：现在写的是“推流数据在 Cache 中设保留时间”。

**③ 检索的证据充分性判定与受限改写**

- WDP：评判“能否支持回答”而不只是“相关”；不够就改写一次（Step-back 或 HyDE）再检索；缺条件就澄清；检索正常但没依据就明确返回“证据不足”；HyDE 文本永远不当证据。
- 我们：Memory Service 的检索是单次的；参考文档 [memory-and-retrieval.md](memory-and-retrieval.md) 5.3 提过“小模型改写 2 到 3 个查询变体”，没有实现；Planner 还没接 Memory Service。
- 建议：不在 Memory Service 里再造一个循环，而是把“检索 → 评判 → 至多改写一次”做成一个 Recipe（或 template 模式的 Goal Node），评判节点 `role = judge`。缺条件时走已有的 human 节点。失败与无结果严格分开：检索后端失败按附录 D.6 报 transient，不能变成空结果。Memory Service 本身目前不吞错（`service.py` 没有吞异常的分支），调用方接入时要守住这一点。
- 挂在：先接通 Planner 与 Memory Service（S-09 允许 Registry 与 Memory 先用内存实现，Registry 已在 T-08 接上，Memory 还没有；`planner/context.py` 里“Memory Service 还没有实现”的注释已过时）；取证 Recipe 挂计划第 7 节“Memory Service 检索流水线”，评测用 S-08 的 harness。

### 6.2 第二档：同一条链上的细化

**④ 父子分块与父块扩展**：叶子块进 VectorStore，父块进 RelationalStore；同父命中 ≥ 2 个子块才升级，并核对 tenant、文档与版本一致。可以和设计书 I.6 的“上下文前缀”叠加：前缀解决“片段不知道自己在讲什么”，父块扩展解决“片段太碎”。随 ① 一起做。

**⑤ 模型 Rerank 与显式降级**：我们的重排现在是查询词覆盖率加稠密相似度（`service.py` 顶部注释），设计书 I.6 第 4 步写的是 cross-encoder 或小模型。这是实现与设计书的一处差距。建议经 Model Gateway 调轻档模型重排；失败时退回召回顺序，在结果的 meta 与 trace 里记 `rerank_degraded`，评测时单独统计降级比例。挂计划第 7 节“Memory Service 检索流水线”。

**⑥ 检索段独立评测**：设计书 I.8 要求召回段与重排段分别测 recall@10、nDCG@10，目前没有实现（仓库里搜不到）。WDP 的做法是检索段 Recall、MRR、NDCG 与答案段正确性、依据性、完整性分开打分。建议在 `agentruntime_learning.evals` 加一个 `retrieval` 子命令，标注集“查询 → 应命中条目”，同样分开发集与留出集。挂 S-08 的扩展。

**⑦ 来源编号与引用校验**：WDP 在一次 Run 内给来源编 S1、S2，答案引用编号。我们 `web_fetch` 与 `context_read` 的结果已有 obj:// 路径，可以在 Task 内给每个来源分配稳定编号，deliver 节点按编号引用，Reviewer 的硬信号检查“每个编号都存在、每条结论至少一个编号”。挂 T-10（工具）与 S-10（Reviewer 硬信号）；编号格式进 /schemas，要走 S-01 流程。

**⑧ 重试只有一个所有者，deadline 覆盖排队**：我们的网关会重试，activity 也会重试，笔记里只写了“总次数要心里有数”。建议明确规则：model 类 activity 的 RetryPolicy 只对“网关不可达”重试，供应商侧的 transient 只由网关重试；网关并发池的排队时间也从调用方 deadline 里扣。挂 R-02 的后续（Model Gateway）。

### 6.3 第三档：做法上的小借鉴

- **ADR 目录**：WDP 用 ADR 记实现期的设计决策（如 ADR0026）。我们的设计书是导出副本不能直接改，建议的改动目前散在回复与计划文档里。可以建一个 `docs/adr/`，每条写背景、决定、对设计书的建议改动、状态（待人更新原文档或已并入），作为设计书更新前的暂存处。
- **从 /schemas 生成类型**：WDP 用 JSON Schema 生成 Python 与 TS 类型。等有 SDK 或前端时再做；要引入代码生成依赖，先按 4.15 说明理由。
- **SSE 客户端约定**：WDP 前端那几条规则（按序号去重、补缺、断线不等于结束、停止要等确认）可以写进 Task API 的契约说明，给以后的客户端用。
- **刷新令牌与 SQLGlot**：等 Ingress 生产实现、出现 SQL 工具时再参考。

## 7. 不借鉴的

| WDP 的做法 | 不借鉴的原因 |
| --- | --- |
| 自建租约、心跳、fencing、orphan 终结 | Temporal 已经提供，而且我们能续跑，不用终结 |
| 自建 RunCheckpoint 与 resume 接口 | human 节点挂起加信号已覆盖，事件历史就是检查点 |
| LangChain、LangGraph 作核心编排 | 设计书的边界是 Task Loop 自研，现成 SDK 只能作 tool_loop 等 mode 的一种实现（速查第 5 节） |
| Run 执行器放在 API 进程 | 我们的 Worker 独立部署、按队列分池扩缩 |
| 四个固定的模型角色 | 我们的 role × Tier × Model Profile 更细，且按期望成本选档 |
| 前端同源托管、Compose 只拉基础设施 | 部署形态不同，我们用 Helm |

## 8. 涉及设计书的改动（待人确认）

设计书是导出副本，以下只列建议，由人更新原文档：

1. **4.5、4.14**：事件正文改为落 RelationalStore，Cache 只做通知与广播；区分“必达”事件与“可丢”的增量事件（对应 ②）。
2. **I.6**：给“Workspace 知识”补版本与发布：Document 与 Document Version、先建后切、检索按当前版本过滤、Task 内固定版本范围；补父子分块与父块扩展（对应 ①、④）。
3. **I.6 第 4 步**：重排失败的降级要记录在结果与 trace 里（对应 ⑤）。
4. **H 或 3.3.1**：写明重试的所有者与 deadline 的分配（对应 ⑧）。
