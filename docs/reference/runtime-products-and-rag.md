# 在本 Runtime 上重写 Aster Code 与 Aster Care，以及 RAG 方向的借鉴

> 前提：本项目是 Aster Runtime 的重写。依据是 [Aster 项目](aster-project.md)、[WDP 项目](wdp-project.md) 两份整理，设计书 1.1、1.2、3.1、附录 D、G、I、K、L，以及 2026-10-24 main 上的代码。
> 这是建议，不是计划：计划没有改，涉及设计书与契约的改动列在第 6 节，等人确认。
> 贯穿全文的一条：我们是 Runtime，WDP 是一个知识库应用，Aster Code、Aster Care 是产品。从它们借东西时，先问“这是两个以上产品都要的、关于安全、资金或恢复的确定性规则吗”。是，就做成 Runtime 的通用能力；只是领域语义，就留给产品。

## 0. 边界：哪些归 Runtime，哪些归产品

设计书 1.2 把“产品界面与领域业务逻辑”划在 Runtime 之外，产品经 Task API 调用 Runtime；1.1 的验收是“新增一类任务只需新增 Node Template 或 Recipe，不改框架代码”。所以重写的检验标准是：**做 Aster Code、Aster Care 时如果要改 Task Loop 的代码，说明 Runtime 缺一项通用能力，应该补成通用能力，而不是给某个产品开特例。**

| 产品要提供的 | 在本 Runtime 里的落点 |
| --- | --- |
| Playbook | Recipe（Registry 发布，Task 提交时固定版本） |
| 角色（Ask、Plan、Build、Review；医生、工程师等专家） | Node Template + Reasoning Template + 租户策略里的工具集与 effect 上限 |
| 专家的“范围卡” | output_schema + judge 节点的验收条件 |
| 工具集 | ToolDriver 登记（effect，以后加 recovery） |
| 上下文预设 | Context Builder 的工作模式（附录 G.3） |
| 领域服务（补丁与 Apply、action-gateway、提醒调度、估价） | 产品自己的服务，经 ToolDriver 暴露成工具，不进 Runtime |
| 客户端（IDE、网页、手机、聊天应用） | Task API（HTTP POST + SSE）与 HumanChannel 的实现 |
| 评测集 | 按产品的任务族建评测集，进 Learning Pipeline |

产品代码建议放在独立的仓库，只依赖本仓库的 proto 与 Task API。在本仓库加 `/products` 一类目录要先经人确认（仓库结构按设计书 4.15）。

## 1. Runtime 先要补的通用能力

两个产品都依赖下面这些能力，应该先在 Runtime 里做，产品再用。

| 编号 | 能力 | Code 为什么要 | Care 为什么要 | 我们现在 |
| --- | --- | --- | --- | --- |
| R1 | **数据版本固定**：Task 开始时固定它依赖的数据版本，记进 Task State 的 pinned | 任务读某个 commit 的冻结快照 | 知识只读当前发布版；售后按下单时生效的规则版本判断 | 只固定 Registry 修订号（3.1.2），不固定数据 |
| R2 | **预算先预留、后结算** | 子任务从同一预算根分份额 | 资金动作并发时不超额 | 只比较“已花”，并发 Node 可能超支（见 [Aster](aster-project.md) ①） |
| R3 | **recovery 维度与“结果未知”** | 推送、开 PR 丢了回执 | 退款丢了回执，按动作 ID 查询，绝不重发 | effect 三档，没有“结果未知”（Aster ②） |
| R4 | **审批绑定输入摘要** | 审批卡片绑定 diff digest | 一键确认绑定订单版本与金额 | `executor/tool_gate.py` 只核对审批属于这次调用（`<调用 id>.1`），不核对输入摘要 |
| R5 | **委托主体** | 云端的事件触发以“规则所有者”的身份运行 | 同一时间代表消费者、客服、商家或运营之一 | 租户上下文只有 tenant 与 user |
| R6 | **Sandbox 的生产实现与执行规格**；出口代理注入凭据 | 在隔离环境里跑测试、构建 | 隔离解析用户上传的附件 | Sandbox 只有本地子进程的开发实现；节点代码拿短期凭据 |
| R7 | **PII 令牌化**：模型只看到令牌，真正需要原值的工具在 ToolDriver 边界还原 | 较少 | 电话、地址、身份证件、支付信息 | 只有写记忆时脱敏（S-13） |
| R8 | **事件正文落库，增量事件可丢** | IDE、网页断线重连 | 网页、手机断线重连 | 事件正文只在 Cache（见 [WDP](wdp-project.md) ②） |
| R9 | **上下文快照** | 排查改错了哪一步 | 排查说错了哪句话 | 不记录取舍（Aster ③） |

R1 是新提的，展开说一下：Registry 固定解决的是“行为版本不在 Task 中途变化”，数据版本是同一个问题的另一半。做法是让 pinned 除了 Registry 修订号，再记“数据源 → 版本”：代码仓库记 commit，知识库记语料修订号（见 4.3），业务规则记生效时间点。工具与检索按 pinned 读数据，Reviewer、回放与评测看到的就是同一份数据。

## 2. Aster Code

### 2.1 映射

| Aster Code | 在本 Runtime 上 |
| --- | --- |
| PREPARING 冻结一切 | Triage 时按 R1 固定 commit，工具只读这个 commit |
| PREPARATION_REJECTED（不建 Run、不花钱） | Task Service 准入阶段拒绝（租户配额与策略），Planner 之前就结束 |
| Ask、Review 角色 | 只读 Recipe；effect 上限 read，按 3.1.1 作为只读 Task 并行，不改 Session 状态 |
| Plan 角色 | 只读 + deliver 一份计划；需要时加 executor = human 的 gate 让人确认 |
| Build 角色与 bugfix Playbook | Recipe：调查（fetch，并行 repo_grep、repo_read）→ 方案（plan）→ 修改（act，提交补丁提案）→ 验证（program，在 Sandbox 里跑检查）→ Reviewer 判定 |
| 修复次数有上限 | Reviewer 判 repair，次数按升级顺序（附录 B.8）封顶 |
| 检查结果 UNKNOWN 时停止循环 | 依赖 R3：结果未知判 escalate，不判 repair 再跑一次 |
| PatchSet、提交与 Apply | 产品的补丁服务给出三个工具：`propose_patch`（write_reversible，只写任务分支或暂存区）、`apply_patch`（write_reversible，逐文件带记录；租户策略要求人工 gate，即“模型提议、人来应用”）、`publish`（推送与草稿 PR，write_irreversible，recovery = queryable，按远程 ref 查询） |
| 五路代码召回 | 设计书 I.6 规定代码默认不建向量索引；先做 grep、glob（已有 repo_grep）与符号、语言服务器工具；稠密召回作为租户可开的选项，要明确的源码上传授权 |
| 行内编辑 | 单次模型调用，走 fast Triage，不经模型规划 |
| 云端：GitHub 事件、聊天、定时任务触发 | 产品的触发服务以委托身份（R5）调 Task API；低频的定时触发用 Temporal Schedules |
| 云端：一次性 microVM、令牌只在出口代理 | R6 |
| 自主等级 L0–L4 | 每个 Workspace 一份租户策略：effect 上限 + gate 策略；L2 的“挣得”见 3.1 对 A3 的讨论 |

已有基础：T-13 真模型端到端的 CI 修复样本（`tests/e2e/live/workspaces/ci_rounding`）已经用 repo_* 工具与 shell_run 跑通了一次最小的“读代码 → 改 → 跑测试”，重写可以从这里起步。

### 2.2 不照搬

- **Workspace Manager 的四类空间与自己的 revision、CAS 协议**：对代码来说 Git 就是权威，Aster 的云端版自己也这么定。Runtime 只固定 commit（R1），产物放 ObjectStore；不在 Runtime 里再做一套版本控制。
- **自建 Journal、租约、epoch**：Temporal 已经覆盖。
- **Strategy 的拓扑库**：用 mode、quality、reasoning（对照见 [Aster](aster-project.md) 5.1）。
- **编辑器桥接的代次与租约**：客户端的事，不进 Runtime。

### 2.3 建议顺序

1. Ask、Review 两个只读 Recipe，用现有的 repo 工具；按 ci_rounding 的形式补评测样本。
2. R1（固定 commit）+ 补丁服务的 propose 与 apply（人工 gate）。
3. bugfix Recipe，需要 R6 的最小实现（容器即可）。
4. 云端：R5 委托身份、触发服务、出口代理。

## 3. Aster Care

### 3.1 映射

**动作等级 A0–A5 = effect × gate 策略 × 额度**

| 等级 | 在本 Runtime 上 |
| --- | --- |
| A0 读取与讲解 | effect = read |
| A1 可撤销、只涉及本人 | write_reversible，自动执行，记录回滚方式 |
| A2 影响订单、可撤销 | write_reversible + 用户确认的 gate；确认要绑定订单版本与金额（R4），执行时产品服务再用 If-Match 核对订单版本 |
| A3 额度内的资金动作 | write_irreversible + **策略 gate**（见下面的冲突） |
| A4 高影响 | write_irreversible + 人工 gate；超更高额度要两人批准（HumanChannel 需要支持多人审批） |
| A5 永不 | 该租户的 Node Catalog 与 ToolDriver 里根本不登记，Policy Engine 直接 deny |

**与设计书的冲突**：Care 的 A3（额度内自动原路退款）与设计书附录 D.4 冲突。D.4 写的是 write_irreversible“执行前插入人工审批”；CLAUDE.md 写的是“执行前必须有通过的 gate 记录”，没说必须是人。要支持 A3，D.4 得加一种“策略 gate”：只对租户策略里白名单的能力开放，额度之内，并且这项能力已经“挣得”自主（影子 → 抽样 → 自动，指标由 Learning Pipeline 算、管理员在 Registry 发布新的策略版本）；指标变差自动退回人工 gate。在设计书改之前，A3 只能按 A4 走人工审批。

**其余部分**

- **专家角色与范围卡**：每个专家是 Node Template（generate）；范围卡写成验收条件；judge 节点（role = judge，换一家模型）逐条核对结论与引用；红色警示由 Policy Engine 的 Hook 触发，Reviewer 判 escalate。
- **专业报告**：Recipe，章节 Node 并行撰写 → judge 核验 → deliver；成品按数据版本存 ObjectStore。
- **投诉**：Care 的“五维分诊”是一个 classify 模型节点（轻档），和我们的 Triage（决定规划深度）不是一回事，命名上要分开。合并与聚类放在产品服务里，在提交 Task **之前**做：一个聚类一个 Task，按订单用 sub_task 扇出，受租户并发上限约束。
- **退款 saga**：业务幂等键（索赔 ID、动作 ID）由产品的 action-gateway 管。我们的 Node 幂等键由 tenant、task、node 与输入哈希生成，只能在一个 Task 内去重，挡不住“两个渠道各开一个 Task 退同一笔款”。所以工具要把业务动作 ID 传给 action-gateway，结果未知时按 R3 用这个 ID 查询。每个订单单写入者、额度上限也在 action-gateway 里；产品服务愿意的话可以自己用 Temporal 工作流（Workflow ID = 订单号）得到单写入者。
- **定时提醒不应该是 Task**：设计书 1.5 的 Temporal 设计吞吐是峰值 1,200 Actions/s，一个 Task 至少要好几次动作；1,000 万条提醒就算每条只算一次动作，也要两个多小时。提醒由产品的调度服务直接发通知；只有需要 agent 推理的情况（比如条件下单前要判断）才提交 Task。
- **转人工**：Verdict escalate + HumanChannel。“发送者 epoch”（人接管后迟到的 AI 输出只当内部草稿）属于产品的会话服务：它决定 deliver 的内容能不能对外发出，Runtime 只负责把 deliver 推给它。
- **知识召回**：见第 4 节；来源优先级（平台 → 地域 → 品类 → 商家 → 店铺）与“按下单时生效的规则版本”用 R1 的 pinned 表达。
- **电商平台合同与记录桩**：做成 ToolDriver 的开发实现，记录每次调用、按故障计划注入故障，和我们 S-07 的外部 ledger 是同一种做法。

### 3.2 不照搬

- **Care 的十几个微服务**：怎么拆是产品自己的事。做演示时，一个产品服务加几组 ToolDriver 工具就够了。
- **条带化的额度计数、分片时间轮、cell 与多地域**：产品的规模问题，不进 Runtime。

### 3.3 建议顺序

1. 平台合同与记录桩（只读的 A0 工具）+ 第 4 节的知识召回。
2. R5 委托主体、R4 审批绑定、action-gateway，先做 A2。
3. 设计书确认“策略 gate”之后做 A3，先跑影子模式。
4. 投诉聚类、专家角色与 judge、报告。

## 4. RAG：参考 WDP 的 L1/L2/L3，但按 Runtime 的方式改

### 4.1 WDP 的分块放到我们这里有什么问题

WDP：L1 2,400 字符（重叠 400）、L2 1,600（重叠 200）、L3 800（重叠 100）；L3 嵌入后进 Milvus，L1、L2 进 Postgres；检索时同一个 L2 下命中 ≥ 2 个 L3 就换成 L2，必要时再升到 L1。

1. **中间层的收益有限**：L1 只是 L2 的 1.5 倍，一个 L1 大约只装得下一个半 L2，第三层带来的上下文很少，却多了一层身份与合并逻辑。
2. **按字符计**：800 个中文字符与 800 个英文字符差好几倍 token，而我们的 Context Builder 按 token 算预算（`AR_CONTEXT_EFFECTIVE_TOKENS`）。
3. **父块是固定窗口，不是文档结构**：固定窗口会把一个小节、一张表切到两个父块里。
4. **在检索里合并，绕过了上下文预算**：WDP 的检索器自己决定交给模型多少文字；在我们这里，上下文预算归 Context Builder（五个分区与 Compaction Ladder），检索器不该替它做决定。
5. **Milvus 原生 BM25 要把明文交给向量库**：我们的记忆正文按租户密钥加密，向量库只拿到向量与可过滤的元数据。稀疏向量要在服务端算好再写入（现在就是这样）。

### 4.2 建议：两层结构，按需展开

- **叶子块（检索单位）**：按 token 计，约 300–500 token，小重叠；沿段落、句子边界切，表格与代码块不切断。具体数值是检索预设的参数，由评测定，不写死。
- **父块（展开单位）**：取文档结构，即标题层级下的小节、页、表格；超过上限再按 token 切。需要更大的上下文时，往上一层是“整篇文档”（标题 + 摘要），不是固定窗口的 L1。
- **叶子入索引的文本 = 上下文前缀 + 正文**：前缀先用免费的“文档标题 + 小节路径”；再按租户预算可选加一句模型生成的说明（设计书 I.6 的“片段 + 上下文前缀”，即 Contextual Retrieval；用轻档模型，记在后台任务的预算上）。
- **检索只返回叶子与父块地址，不在检索里合并**。展开有两条路：
  1. Context Builder 按预算决定是否把同一父块下命中 ≥ 2 个的叶子换成父块。WDP 的合并条件保留为默认策略，但由 Context Builder 执行、计入预算。
  2. agent 用只读工具按地址取父块，与现有的 `context_read` 按 obj:// 路径取回外置内容是同一个模式。

  这样小上下文的模型用叶子就够，大上下文的模型可以多展开，检索结果本身不随模型变化。
- **片段身份**：WDP 的字段（tenant、workspace、document、version、页码、小节路径、层级、父块、内容哈希）加上我们已有的 embedding_version、来源系统的权限（I.6“权限继承源系统”）、sensitivity。chunk_id 按内容寻址（Aster 的做法）：新版本里没变的片段直接复用旧向量，只嵌入变了的部分。

### 4.3 版本、固定与入库

- **版本与发布**：沿用 WDP 的“先建新版本、校验通过再切换”。
- **语料修订号**：每个 Workspace 的知识库有一个单调递增的修订号，每次发布加一。片段带 `[published_rev, retired_rev)`；Task 开始时按 R1 固定修订号 R，检索条件是 `published_rev ≤ R < retired_rev`。好处是不用复制数据就能让 Task 内读到一致的版本；旧版本等到没有运行中的 Task 固定它时再回收。
- **入库是一个工作流**：放在 offline 任务队列（附录 K.5）上的 Temporal 工作流，而不是 WDP 那样带租约表的 Indexing Worker。步骤：解析（program 节点，在 Sandbox 里跑，用户上传的文件不可信）→ 分块 → 可选的上下文说明（model，轻档）→ 经 Model Gateway 嵌入（计量；Memory Service 现在的 `GatewayEmbedder` 已经这样做）→ 按 chunk_id 幂等写 VectorStore 与 RelationalStore（大正文放 ObjectStore）→ 核对清单 → 在 RelationalStore 一个事务里加修订号。
- **解析器是插槽**：PyPDF、Unstructured 这类解析库不在设计书 4.15 里，引入要先说明理由。建议 Runtime 只定义解析器接口，开发实现只处理 Markdown 与纯文本，具体解析器由部署方或产品提供。

### 4.4 检索与取证

- **检索预设**（Aster 的做法）：同一条检索流水线，三组参数。
  - `memory-default`：现在的流水线，带信任与新鲜度衰减。
  - `knowledge-default`：不按新鲜度衰减；按来源优先级排序；精排后做权威复核（权限、版本、删除）；返回引用。
  - `code`：grep、符号优先，不用向量。
- **复核与去冗余的顺序**、**精排经网关并记录降级**：见 [Aster](aster-project.md) ⑥ 与 [WDP](wdp-project.md) ⑤。
- **证据够不够、改写一次再检索**：做成 Recipe，见 WDP ③。HyDE 一类改写产出的文本标为“派生”，不能被引用。
- **引用合同**：Reviewer 只接受地址是“本 Task 检索到的片段”的引用（WDP ⑦）。
- **失败不等于没有**：检索后端出错报 transient；后端正常但没有命中，返回“证据不足”。Reviewer 要区分这两种情况，不能把网络故障判成“知识库里没有”。
- **评测**：检索段 Recall@K、MRR、nDCG@K，答案段的依据性与完整性分开打分（WDP ⑥）。可以拿 WDP 的多文档问题（北仓部署 24 台 Orion V2、Pro 套餐、4 小时响应）作为第一批种子样本的形式。

### 4.5 WDP 其他值得带过来的设计（Runtime 化之后）

| WDP 的设计 | Runtime 化之后的做法 | 不照搬的部分 |
| --- | --- | --- |
| Event 与状态同事务落库，Outbox → Redis 通知 | R8：节点级事件落 RelationalStore，token 增量走 Cache、可丢 | Redis Streams 这个具体实现 |
| Run 冻结模型快照 | 已有：Registry 修订号固定 | — |
| 一次调用只有一个重试所有者 | 写明 activity 与网关各自重试什么（WDP ⑧） | — |
| Rerank 失败要记录降级 | meta 与 trace 里的降级标记 | — |
| 多事实问题拆成子问题并行，检查覆盖缺口 | Planner 规划并行 Node，每个子问题一条验收条件，由 Reviewer 检查覆盖 | LangGraph 的 Send 子图 |
| 澄清后恢复同一个 Run | human 节点挂起，回复以信号送回 | 自建 RunCheckpoint |
| Web Research 的来源编号 | 引用合同 | Tavily 等具体服务 |

### 4.6 不搬的

固定在工具里的 9 节点 LangGraph 取证图；按字符的固定窗口三层分块；向量库原生 BM25（要明文）；在 API 进程里加载 BGE-M3（我们经 Model Gateway 嵌入）；FastAPI BackgroundTasks；把 Run 执行器放在 API 进程。

## 5. 能不能用 Milvus

> 2026-10-24 用户决定默认改用 Milvus，设计书的改动草稿见 [design-changes-2026-10.md](../plan/design-changes-2026-10.md) 的 C1。下面保留决定前的分析；其中第 3、4 条（不用原生 BM25、加密与变换要重做）在 Milvus 作默认时仍然成立，已写进 C1。

能，但建议作为 VectorStore 的第二个实现，不替换 Qdrant 当默认。

**为什么能**：`libs/py-core/.../providers/vector_store.py` 的接口与具体产品无关：按语料写入、按 id 取回与删除、稠密 + 稀疏查询并用 RRF（k = 60）融合、标量与 AnyOf 过滤、按 embedding_version 隔离。这些 Milvus 都能做：同一集合里放稠密与稀疏向量、混合检索带 RRF、用 partition key 按租户分区、数组字段的“包含任一”过滤。核心代码只依赖接口，换实现不用改核心。

**为什么现在不换默认**：

1. **设计书已有结论**：4.4 与附录 L.4 采用 Qdrant，向量超过 1.5 亿条时再评估 Milvus（GPU 索引、亿级规模）。我们离这个量级很远。
2. **运维与测试成本**：Milvus 单机版要 etcd 与 MinIO（WDP 就是这样部署的），集群版组件更多。我们的测试靠 PATH 上的单个 `qdrant` 可执行文件拉起临时实例，本机是 Windows，换成 Milvus 基本要靠 Docker，现有的测试方式要改。
3. **WDP 用 Milvus 的主要理由在我们这里不成立**：它用的是 Milvus 原生 BM25，从明文分词。我们的正文按租户密钥加密，向量库不拿明文；稀疏向量由服务端算好写入，用 Qdrant 还是 Milvus 都一样。
4. **加密与变换要重做一遍**：接口注释写明“向量按租户密钥加密存储由生产实现负责”。Milvus 实现要自己做租户分区、强制过滤与向量的保距变换，并通过 S-02 的一致性测试与 S-12b 的越权测试。
5. **新依赖要说明理由**：Milvus 不在设计书 4.15 的选型里。

**什么时候值得做**：客户或租户已经在运营 Milvus，部署上必须用它；或者向量规模接近设计书的演进阈值。做法：写 `adapters/milvus.py` 实现同一接口；一致性测试与越权测试跑在 Milvus 上；用配置切换实现；设计书 L.4 补一句“租户已有 Milvus 时可用第二实现”。

## 6. 涉及设计书与契约的改动（待人确认）

设计书是导出副本，以下只列建议，由人更新原文档。契约变更走 S-01 流程。

1. **3.1.2、附录 B.1**：Task State 的 pinned 扩展到数据版本（R1）。
2. **附录 D.4**：write_irreversible 的 gate 允许“策略 gate”，限定能力、额度与挣得的自主权（Care A3）；不改的话 A3 只能走人工审批。
3. **附录 D.5、4.11**：gate 记录带输入摘要（R4）；HumanChannel 支持多人审批。
4. **1.3 与租户上下文**：增加委托主体（R5）；租户上下文的 proto 要改。
5. **4.10、4.12**：Sandbox 的执行规格；出口代理注入凭据（R6）。
6. **新增 PII 令牌化**（R7），放在 ToolDriver 与 Context Builder 的边界。
7. **附录 I.6**：文档知识的两层分块、按需展开、语料修订号、入库工作流、检索预设。
8. **附录 L.4**：Milvus 作为第二实现的适用条件。

前两份整理里的建议（事件落库、预算预留、recovery、上下文快照等）也都还在等确认，见 [WDP](wdp-project.md) 第 8 节与 [Aster](aster-project.md) 第 8 节。
