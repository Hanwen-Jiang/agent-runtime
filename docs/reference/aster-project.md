# Aster 项目（Runtime、Code、Care）：总结与可借鉴之处

> 来源：`Z:\Aster\Aster-artifacts` 里的设计视图（仓库名 dag-code-agent，2026-10-11 至 10-15），读的是中文版的 Markdown 和其中几张架构图：《Aster Runtime 架构》《Aster Code 架构》《Aster Code 云端架构》《Aster Care 架构》《Aster 检索召回图》。同目录另有《Aster 集群拓扑》《看懂 Kubernetes 集群》，本文没有展开。
> 这些都是设计，不是现状：原文写明仓库代码在多处落后于设计，云端版与 Care 的内容“都还没有实现或实测”，设计目标里的数字也都没有实测过。
> 本项目一侧的现状核对于 2026-10-24（main）。术语按设计书附录 A；Aster 自己的名称（Run、Operation、Strategy、Playbook 等）保留原名，对照见第 5 节。

## 1. 三者的关系

| 部分 | 是什么 | 一句话 |
| --- | --- | --- |
| Aster Runtime | 共用的执行底座，七个模块 | Strategy 提议，Kernel 决定；权限只会收窄；每类事实只有一个所有者 |
| Aster Code | 建在 Runtime 上的编码工作台，桌面版与云端版两种部署 | 模型提议、人来应用；一切都有版本；副作用可对账 |
| Aster Care | 建在 Runtime 上、服务大型电商平台的服务智能体 | 真相留在平台；委托而非超级用户；自主权靠挣得 |

产品只添加领域语义：Playbook、角色、工具集、上下文预设和客户端。Run、Workspace、执行、恢复和授权都来自 Runtime，产品不另带一套执行内核。

## 2. Aster Runtime

### 2.1 六条原则

1. **Strategy 提议，Kernel 决定**：Strategy、模型和工具只返回请求；授权、预算、版本和执行都归 Kernel。
2. **权限只会收窄**：租户上限 → 角色上限 → 用户授权 → Workspace 范围 → 工具需求 → 审批，每一步只能缩小上一步的结果，结果为空即拒绝。
3. **每类事实只有一个所有者**：执行历史归 Journal，文件与版本归 Workspace，生命周期事实归 Memory；索引都是派生数据，可以重建。
4. **执行严格按计划**：执行模块只运行带 digest 的冻结 ExecutionPlan，绝不扩大文件、网络、秘密或资源范围。
5. **产品只添加领域语义**。
6. **预设优先，插槽可替换**：每个模块自带预设；开发者可以在声明的插槽里替换实现，但不能跳过固定步骤。

### 2.2 七个模块

| 编号 | 模块 | 职责 | 关键规则 |
| --- | --- | --- | --- |
| M1 | In/Out | 每类调用方一个协议适配器；认证器只输出已验证的 Principal | 统一的 Run 协议：Start、Get、Events、Signal/Approve、Cancel；写操作带幂等键，同键不同正文视为冲突；持久事件按 Run 有游标可续读，流式增量可以丢，重连从已提交状态补齐 |
| M2 | Harness · Kernel | 确定性的一半：允许什么、给多少、发生了什么 | 六个子模块各有只能由自己写的数据：运行引擎与恢复、租户与授权、资源、Workspace Manager、Registry、子代理与审批 |
| M2·1 | Kernel · Workspace Manager | 角色、用户、项目、运行四类空间 | Run 开始时冻结叠加视图；提交要租约（TTL 30 秒，最长 5 分钟）+ fencing epoch + 清单 digest + CAS 更新 HEAD；冲突时三方合并或报冲突；提交与 Apply（写回真实目录）是两个动作 |
| M2·2 | Harness · Strategy | 可替换的一半：下一步做什么 | 纯函数 `Decide`，只返回 Command、等待或结束；自己不调模型、工具或子代理；状态可序列化由 Kernel 保存；随机数以 Run 为种子 |
| M3 | Context | 编译每一次模型输入的“没有权限的编译器” | 产出不可变的 ContextSnapshot，记下放进了什么、漏掉了什么、为什么 |
| M4 | Memory | 工作记忆、长期文本、长期向量三层 | 每条事实一个权威存储；模型提议的记忆默认要用户确认；秘密、支付数据、身份证件一律拒绝 |
| M5 | Tools | 带 schema 和副作用类别、开放给模型的逻辑 | 工具自身不持有凭据；`effect` 与 `recovery` 两个维度决定崩溃后能否重试 |
| M6 | BEM（基础执行模块） | 唯一接触系统与环境的模块 | 七种执行 profile；所有外发流量经出口代理；后端不可用是能力错误，从不静默退回更弱的 profile |
| M7 | LLM | 每种 SDK 一个 Provider 插槽 | 模块只请求模型 profile，租户把 profile 映射到插槽；Embedding、精排也是插槽，成本记在同一处 |
| M7.1 | LLM · 判定插槽 | 类型化判定（选择、打分、布尔概率） | 判定结果只能收紧或升级，概率永远不变成授权 |

依赖只向下：只有 Kernel 调用其他模块。不可信代码从不在 Runtime 进程内运行。CI 里的架构测试强制 import 规则：harness 下的包不得 import `os`、`net`、`net/http`、`database/sql`。

### 2.3 一次决定、一次派发：四个短事务

```text
事务 1：worker 获取 Run 租约，读取状态
Decide：在任何事务之外调用 Strategy，得到 Decision
事务 2：检查 epoch，把 Command 落成有稳定 ID 的 Operation
事务 3：dispatcher 准入、预留预算、创建 Attempt、生成计划、写 outbox
发送：带幂等键与截止时间发给 LLM 或 BEM（在任何事务之外）
事务 4：按 Attempt + revision 接收回执、结算预算、唤醒下一个角色
```

- 每个 Run 同一时间只有一个写入者（租约 + 所有者 epoch）；每个 Workspace 分支只有一个写入者（提交租约 + fencing epoch）。
- 任何事务、任何进程内锁都不跨越模型调用、子进程或网络请求。
- 提交时唤醒下一个角色，轮询只是兜底（AD-11）。

### 2.4 有效能力：每一步只能收窄

```text
TenantSpace（租户上限、路由、限额）
∩ 角色预设（role.yaml 锁定字段）
∩ 授权（用户或会话的 CapabilityGrant）
∩ Workspace ACL（空间、路径、读写）
∩ 工具需求（ToolSpec 的 capabilities）
∩ 审批（需要时）
= ExecutionPlan（冻结、带 digest）→ BEM 按计划执行，不多做任何事
```

结果为空即拒绝（CAPABILITY_DENIED、ROUTE_NOT_ALLOWED、RESOURCE_LIMIT_EXCEEDED），不会退回到更弱的 profile。三道防线：Kernel 的授权、BEM、后端自身的隔离。

### 2.5 Strategy：拓扑、增强器与 Playbook

| 层 | 内容 |
| --- | --- |
| 拓扑（控制结构） | ReAct、Plan-DAG、Loop + Verifier（运行 → 验证 → 有界修复）、思维树、扇出 + 裁判、辩论、研究循环、Swarm（协调者 + 角色子代理） |
| 增强器（可包裹任何拓扑） | 反思（批评 → 修改）、CoT（模型有 thinking 设置时映射到它）、投票（采样 → 投票） |
| Playbook（按任务类别的预设） | deepsearch、研究综合、Code 的 bugfix / feature / review、Care 的售后 / 退款 |

Plan-DAG 的流程：

```text
分类（先规则，模型兜底；置信度低时问用户或改用 ReAct）
→ 取 Playbook 模板（节点类型、可改范围、限额）
→ 模型只返回对模板的修改
→ 校验（无环、工具在角色内、预算）
→ 冻结一个版本（可选用户审批）
→ 执行节点（子 Run、Operation）
→ 检查点：继续、重规划或结束；重规划产生新版本，已完成且输入没变的节点直接复用，默认最多 2 次
→ 先过 Verifier，再结束
```

子代理就是子 Run：授权是父级有效能力与角色上限的交集，默认深度 1、并行 4，只返回结果，控制权从不转移。

### 2.6 Context：六步编译

```text
1 收集候选（可扩展：ContextSource）
2 授权与限定范围（固定：租户、授权、revision、墓碑）
3 选择与排序（可扩展：召回预设、ContextPolicy）
4 压缩（可扩展：压缩预设、tokenizer）
5 校验不变量（固定：配对、预算、受保护段）
6 持久化 ContextSnapshot（固定：digest、预设、遗漏原因）
```

- 压缩从无损到有损分五级：L0 去重被取代的结果 → L1 外存（头尾 + 引用）→ L2 从 Journal 折叠步骤 → L3 计费的模型摘要 → L4 缩减召回。受保护段（系统约束、当前目标、未完成动作、待审批事项、UNKNOWN 副作用、工具 schema）永不压缩。
- 子代理不共享对话记录：父级发 TaskBrief（目标、检查、输入@revision、预算），子代理返回有界的 HandoffResult（摘要 ≤ 4 KB、产出、证据、未决问题、副作用）。
- 检索与记忆来的材料一律标为外部数据，永远不变成系统指令或权限来源。
- 预设按 Playbook、策略、角色选：coding-edit、coding-review、plan、research、care-support、subagent-child、resume。

### 2.7 Memory

- 工作记忆以 Journal 为后盾，Redis 只是多实例时可选的热缓存，丢了只增加延迟。
- 长期文本是用户、角色、项目空间里的 Markdown，人可以直接读和改；语义归 Memory，文件由 Workspace Manager 提交。
- 程序按键读取的结构化事实（语言偏好、同意标记）是带 CAS 和墓碑的 MemoryItem；同一事实不两边都写。
- 崩溃恢复只读 Journal，从不读 Memory（AD-6）；Memory 只用于“从上次停下的地方继续”。

### 2.8 Tools 与 BEM

- 工具声明两个维度：`effect`（pure、read、write、external）和 `recovery`（pure、idempotent、queryable、nonrepeatable）。后者决定崩溃后哪些调用可以重试、哪些要按原 Attempt 查询、哪些只能标 UNKNOWN。
- `profile` 与 `capabilities` 告诉 Kernel 这个工具要哪个执行 profile、哪些路径、网络目标与秘密，Kernel 据此算有效能力。
- 执行 profile 从 `wasi-pure`、`wasi` 到 `oci-gvisor`、`microvm`、`remote`、`trusted-host`。完全访问也是一种 profile（隔离为“无”），照样有 Sandbox ID、租户、授权、Attempt、取消和 UNKNOWN。
- 观点：Wasm 让执行可控、固定 digest 让代码来源可控，但都不能让网络副作用可控；真正的控制在出口代理与 Kernel。MCP 是协议，不是隔离技术。

### 2.9 判定插槽：三类判定，三种做法

| 判定类型 | 例子 | 由谁决定 |
| --- | --- | --- |
| 精确判定 | 授权交集、预算与配额、幂等、租约与 fencing、恢复与 UNKNOWN、审批绑定、调度 | 只用 Kernel 代码，不用任何模型 |
| 稳定、高频 | Playbook 分类、Care 五维分诊、专家角色路由、记忆写入路由 | decision 插槽（类型化判定模型）；标签够了换本地分类头 |
| 开放、长尾 | 结论核查、报告评分、异常工具调用审查、扇出与辩论的裁判 | 快路径用决策模型，慢路径用带理由的生成模型 |

一次判定是只读、可重复的 `judge` Operation，响应丢了可以直接重试；Strategy 读到答案后，重放用记录下来的答案。换 Provider 或模型 revision 时，该问题退回抽样复核；阈值只在本地校准、同一评测集比过之后才放行自动执行。

### 2.10 检索骨架（Code 与 Care 共用）

```text
查询（带主体与范围）
→ ① 精确过滤（Runtime 固定）：租户、主体、版本、权限、删除标记
→ ② 多路召回（可替换）：exact、BM25、symbol、dense、lsp，每路只返回排好序的 ID
→ ③ RRF（默认）：只看名次，k = 60
→ ④ 精排（可选）：只给 {id, score}，不增删候选；失败退回 RRF 顺序
→ ⑤ 权威复核（Runtime 固定）：权限、版本、删除；核对正文 digest，从权威快照回读
→ ⑤b 去冗余与截断（可配置）：精确去重、相邻片段合并、每文档上限，MMR 可选、默认关
→ ⑥ 交给 Context（Runtime 固定）：标为外部数据，按预算选，不够就只记遗漏
```

三条规则：先过滤再召回（全局召回再过滤，别的租户的词频会影响排序）；分数只表示相关，不能授权；共享语料与租户语料分路召回。身份与版本：chunk_id 按内容寻址；embedding 身份含模型、维度、度量，任一变化就生成新的 index_version，不混排。降级：可选能力缺失就降级并记原因，显式要求的能力缺失直接报错。评测时 Recall、MRR、nDCG@K 一起看。

### 2.11 数据、部署、信任与恢复

- **数据归属**：Run、Operation、Attempt、事件、审批、outbox、预算都在 SQL 里按租户事务写；文件内容、快照、改动、制品放按内容寻址的 blob 存储，记录只存 digest 与清单；BM25、符号、向量都是派生数据（本地 FTS，服务端 pgvector）。
- **部署形态**：嵌入式与本地（SQLite + 本地 blob，不需要 Postgres、Redis、容器或模型服务）→ 服务端单节点（PostgreSQL + S3）→ 服务端扩展（api、worker、dispatcher、reconciler、indexer 分开扩）→ 混合（云端 Run + 用户电脑上的 trusted-host 代理）。
- **信任边界 B1–B7**：请求正文永远不能指定租户或主体（B2）；模型输出、工具注释、技能与 MCP 文本、检索数据只能构成请求或数据，永远不构成权限（B3）；BEM 只接受带 digest 的计划（B4）；出口代理做白名单、注入凭据、大小限制与审计（B6）；每条记录、索引条目、缓存键、blob 引用都带租户（B7）。
- **崩溃恢复**：新 worker 以更高的 epoch 接管 → 读已提交的 Operation → reconciler 逐个查询已派发的 Attempt → 接收回执或标为 UNKNOWN 并保留预算预留 → 再次 Decide，已提交的 Command 不重发。全程不读 Memory。

### 2.12 架构决定与迁移

12 条架构决定里值得记住的：AD-1 Harness = Kernel + Strategy；AD-3 一个 SQL 事务单元 + 内容寻址存储 + 派生索引；AD-4 只通过冻结的 ExecutionPlan 执行；AD-6 崩溃恢复只用 Journal；AD-7 Redis 等到多实例再引入；AD-10 **自己的 Run 后端是内部步骤唯一的恢复权威，River 只负责投递唤醒，Temporal 只运行外层工作流**；AD-12 判定是 Runtime 合同，概率永远不变成授权。

迁移分 0–10 阶段，每阶段单独验收：模块划分（已完成）→ 移出旧技术栈 → 最小租户控制 → BEM 接口 → Workspace Manager → Registry → 策略库与 Plan-DAG → Memory 分层 → Context 预设 → 第二个 LLM 插槽 → Care 改用共享检索与执行。

## 3. Aster Code

### 3.1 桌面版

五条原则：模型提议、人来应用；一切都有版本（任务读某个 revision 的冻结快照，编辑带前后 hash）；副作用可以对账（先写意图再产生副作用，丢失的回复记 UNKNOWN，按原 Attempt 核对，绝不重发）；项目索引帮人选范围，模型只读冻结的任务快照；产品只有编码语义。

| 组件 | 要点 |
| --- | --- |
| C1 客户端与 Coding API | 桌面、VS Code、网页、CLI 共用一份合同和一种错误封装；订阅事件，不轮询；桌面应用的令牌放系统钥匙串 |
| C2 任务生命周期 | 任务 ID 由租户、主体、Idempotency-Key 得出；模型将看到的一切在 PREPARING 时冻结；超限项目进 PREPARATION_REJECTED，没有 Run、没有费用 |
| C3 角色与 Playbook | Ask、Plan、Build、Review 从“模式”变成角色预设；Build 是分类器选出的 Plan-DAG Playbook（`bugfix.yaml`）；检查仍失败就交付失败证据；检查结果 UNKNOWN 时停止循环，不再触发一次测试 |
| C4 Workspace 与范围 | 项目空间带不可变 revision；worktree 是项目空间的分支；`MEMORY.md`、`AGENTS.md` 由模型提议、用户确认后提交 |
| C5 检索与语言证据 | 五路召回（exact、BM25、tree-sitter symbol、dense、语言服务器）；语义召回要明确的源码上传授权，没有就降级并说明原因 |
| C6 PatchSet、提交与 Apply | 所有改动来源都落成 PatchSet；人批准后 Kernel 提交，Apply 逐文件带 journal 写入真实目录；跨文件没有原子性，靠逐文件进度恢复 |
| C7 验证与执行 | 检查是固定版本的 profile，在副本上跑，从不在项目上跑；证据绑定确切的候选与 revision；后端缺失或 digest 变化即拒绝，不退回本地 shell |
| C8 编辑器桥接 | 服务端只记录应该改什么，扩展执行并报告实际看到的结果；绑定带递增代次与租约 |
| C9 Worktree、发布与交接 | Git 用固定版本的可执行文件，禁用 hooks、外部过滤器、fsmonitor、LFS smudge、凭据提示；发布从不强推、合并或删除远程；提交与草稿 PR 分别审批 |
| C10 技能与 MCP | 技能与 MCP 绑定是 Registry 对象，以文件放在角色空间与用户空间；安装要确切的包、版本、digest；撤销阻止之后的读取与调用 |
| C11 账户与计量 | 每次尝试前预留预算，派发时再查一次撤销；订阅收入、模型成本、沙箱成本三处分开记，从不相互抵扣 |
| C12 包结构 | 产品包按领域拆成小子包，各自用类型化仓储管自己的记录；属于执行、Registry、检索、策略的代码移进 Runtime |

### 3.2 云端版

第二种部署：多租户服务，每个成员有一个住在云端的个人编码智能体，从 GitHub 事件、聊天、定时任务和手机接活，在一次性 microVM 里跑，交回分支和草稿 PR。

- **Git 是权威**：项目在 GitHub 远程仓库；智能体只在 `aster/*` 分支上工作；人来合并。
- **一个 VM 一个任务**：Firecracker microVM 从预热池领取，用完销毁，从不跨租户或跨任务复用；空闲时快照、停止，等 CI 时不付费。三层快照：公共基础镜像、租户的项目预备快照、短期会话快照。
- **免费版同样隔离**：成本靠每日 microVM 运行时长配额、单次会话上限、一个并发沙箱控制，只计 VM 实际运行的墙钟时间；免费配额按 GitHub 身份算，多建组织不叠加。
- **事件说明做什么，从不说明允许做什么**：有效权限 = 规则所有者的授权 ∩ 成员自己的 GitHub 权限 ∩ App 安装范围 ∩ 项目自主上限。自主等级 L0–L4：L0、L1 自动；L2 按项目凭证据晋级；L3 总要人；L4 关闭。审批卡片绑定分支、基础 commit、diff digest 和证据。
- **令牌从不进沙箱**：每任务一个、收窄到一个仓库的 GitHub 安装令牌，只在出口代理处注入；代理读每次推送的 ref，只放行 `aster/*`；默认分支的 ruleset 是第二道防线。BYOK 密钥同样只在出口代理处使用。
- **分布式**：地域边缘、全局控制平面、按组织划分的 cell（各有 Runtime、服务、PostgreSQL、事件分区、仓库缓存）、按地域共享硬件的沙箱集群（专用 KVM 节点池，由节点代理管理，不作为 Pod 调度）。
- **准入顺序**：先租户配额，再按方案加权的公平队列，再按来源的优先级（交互 > 事件触发 > 定时），最后才是容量；只抢占批处理。
- **触发风暴**：一次坏的依赖更新让几百个 PR 失败时，按失败特征聚类，每个聚类只调查一次，再在租户并发上限内按分支扇出；仓库缓存争用用单飞 fetch。
- **预算四层**：组织、项目、个人智能体、任务；接近任务上限时暂停并询问是否追加，没有任何东西静默失败。

## 4. Aster Care

服务智能体，替用户操作电商网站、估价、写报告、讲解原理、推荐、处理投诉、定时提醒。

- **真相留在平台**：价格、库存、订单、支付、退款记录和商家结算都归电商平台；Care 只读数据、发命令，读与写各只有一个入口。
- **委托而非超级用户**：同一时间只代表一个主体（消费者、平台客服、商家、平台运营），能做的不超过该主体本身。
- **动作分级 A0–A5**：A0 读取与讲解、A1 可撤销且只涉及本人 → 自动；A2 影响订单、可撤销 → 一键确认，绑定订单版本与金额；A3 额度内的资金动作 → 策略与证据通过时自动，抽样复核；A4 高影响 → 人批准，超更高额度要两人；A5（改价格库存、动支付记录、绕过身份校验或人机验证等）→ 对智能体关闭。
- **自主权靠挣得**：每项 A3 能力在“影子 → 抽样 → 自动”之间按审计准确率晋级，出现异常自动回落。
- **专家角色带范围卡**：医生、工程师、研究员、估价分析师、消费者权益顾问各有“可以”和“不可以”；判定器逐条对照结论与引用，红色警示（胸痛、高压电等）升级给人。
- **退款 saga**：每个订单一个写入者分区，动作 ID 作幂等键；丢失的响应按同一个动作 ID 查询解决，绝不重发；平台强制 `新退款 ≤ 已付 − 已退 − 有效预留`，Care 自己的额度台账是第二道上限；只有明确失败才释放预留，UNKNOWN 不因计时器到期而失效；租户级热点额度做条带化。
- **投诉**：跨渠道按订单与问题合并 → 有共同根因时聚类 → 五维分诊 → 方案库 → 等级闸门决定 Care 执行、人批准还是转人工；一次承运商事件让上万条投诉变成一个批量方案，审批一次、逐单执行。
- **秒杀提醒**：按到期分钟分区，提前加载进按租约归属的分片时间轮，在确定的时间窗内分散发送，经按渠道的令牌桶；分片归属用与 Run 相同的租约与 fencing epoch。
- **转人工**：技能路由 + SLA 计时器；Care 继续当副驾驶；“发送者 epoch”决定谁能发言，人接管后迟到的 AI 输出只变成内部草稿。
- **数据**：PII 在模型上下文里是令牌，只有真正需要真实值的工具才看得到；数据访问限定用途，跨工单汇总只给运营主体；投诉文本、图片、评价、商家页面都是不可信数据。
- **分布式**：每个服务拥有自己的数据，事务性 outbox 发布、inbox 去重消费、按键保序；读模型加速上下文，但资金决定重新读权威数据；用户有归属 cell。
- **知识召回**：只检索人工发布后的当前生效版本；公共语料与租户语料各走关键词与语义两路、各自排名再 RRF；按“平台 → 地域 → 品类 → 商家 → 店铺”的来源优先级排序，下层可以补充或收窄上层，不能推翻；售后判断取下单时生效的规则版本；默认候选 60 条、返回 8 条。
- **电商平台合同 + 记录桩**：现在平台是一份合同加一个演示实现，记录每次调用、注入故障、播放事件脚本；真实适配器以后替换它，必须过同样的合同测试。

## 5. 对照

### 5.1 概念对照

| Aster | 本项目 | 说明 |
| --- | --- | --- |
| Run、子 Run | Task；sub_agent、sub_task 模式的 Goal Node | |
| Operation、Attempt | Node、Attempt | |
| Journal + 租约 + epoch + reconciler | Temporal 事件历史与重放 | Aster 自建（AD-10），我们采用 Temporal |
| Kernel | Temporal + Stage Runner + Policy Engine + Node Executor 的 effect 规则 | |
| Strategy 的 `Decide` | Task 工作流代码（确定性）+ Planner、Reviewer 的决策 activity | 两边都把“决定”与“执行”分开 |
| Playbook | Recipe | |
| Plan-DAG（检查点重规划） | 每轮只规划到下一个信息检查点；Verdict replan | |
| Loop + Verifier、反思 | quality = revise | |
| 扇出 + 裁判、投票 | quality = ensemble | |
| 思维树 | quality = search | |
| CoT | reasoning = stepwise | |
| Swarm | 无 | |
| TenantSpace、CapabilityGrant、有效能力 | 租户策略（Registry）给出的 effect 上限、模型白名单、预算 | |
| ExecutionPlan + BEM | Sandbox 接口（生产实现待接入） | |
| ContextSnapshot | Context Builder 的输出（不持久化取舍） | |
| decision 插槽 | Triage 的规则路径；Reviewer 的硬信号；judge 换家 | |
| Model profile → Provider 插槽 | Model Profile → Tier → 供应商适配（Model Gateway） | |
| UNKNOWN | “写操作超时且结果未知时，先按幂等键对账” | 我们没有显式的 UNKNOWN 状态 |

### 5.2 谁强在哪

| 维度 | Aster（设计） | 本项目（实现） |
| --- | --- | --- |
| 持久执行 | 自建，能嵌入、能本地跑，不要外部服务 | Temporal，成熟、有重放测试与故障注入，但必须有 Temporal 服务 |
| 权限模型 | 交集收窄链、冻结计划、出口代理注入凭据 | effect 上限、Hook、gate、幂等键；SecretStore 签发短期凭据给节点代码 |
| 副作用恢复 | effect × recovery 两个维度、UNKNOWN 保留预算 | effect 三档；按幂等键对账 |
| 预算 | 每次尝试前预留、之后结算 | 每步之前查“已花”，不预留 |
| 上下文 | 六步编译，固定步骤与可扩展步骤分开，快照记下遗漏原因 | 五区布局、Compaction Ladder、Compaction Probe；有实测评测 |
| 检索 | 骨架完整：先滤后召、精排只打分、权威复核、按 index_version 不混排 | 预过滤、混合召回、启发式重排、信任 × 新鲜度 × 作用域、MMR；记忆治理（晋升、投毒防护、级联删除）比 Aster 细 |
| 多租户 | 每条记录、索引、缓存键、blob 带租户；cell 隔离 | RLS、按租户密钥加密、越权测试进 CI |
| 落地程度 | 设计为主，代码落后，云端与 Care 未实现 | 已实现并有端到端、真模型与故障注入测试 |

## 6. 值得借鉴

### 6.1 第一档

**① 预算先预留、后结算**

- Aster：第一次尝试前预留，每次尝试后按用量结算；子代理份额从同一预算根划出；UNKNOWN 保留其预留。
- 我们：`stage_runner.py` 的 `_budget()` 与 Policy Engine 的 budget_check 只比较“已花”与预算。同一个 Stage 里并发的 Node 会各自通过检查，再一起花钱，可能超支；tool_loop 每一步之前也只看已花的钱。
- 建议：在 Stage Runner 里按 Model Gateway 的期望成本给每个 Node 预留，启动前从剩余预算里扣，结束后按实际成本结算、退还差额。预留只是工作流内的纯计算，不需要 I/O，满足确定性要求；但它会改变哪些 Node 被放行，所以要加版本分支（`workflow.patched`）并补重放测试。
- 挂在：R-01 的后续（budget_check）与 S-04（Stage Runner）。设计书 3.2.3 或附录 F 要写明预留规则。

**② 副作用分两个维度：effect 与 recovery**

- Aster：`effect` 说会不会改外部状态，`recovery` 说结果丢了怎么办：idempotent 可以重发，queryable 要按原 Attempt 查询，nonrepeatable 只能标 UNKNOWN 交给人。
- 我们：effect 三档（read、write_reversible、write_irreversible）同时决定了重试与审批；“超时且结果未知时先按幂等键对账”默认了工具支持按键查询，而 ToolDriver 登记时没有声明它支不支持。附录 D.6 的失败分类里也没有“结果未知”。
- 建议：ToolDriver 登记时声明 recovery；对账路径只对 queryable 开放；nonrepeatable 的工具结果未知时直接按 UNKNOWN 交给 Reviewer（通常 escalate），不重试也不假装失败。
- 挂在：S-05（Node Executor 与 effect 规则）的后续与 T-10（工具）。Node Template 的 schema 要加字段，走 S-01 流程；设计书附录 D.4、D.6 要改。

**③ 上下文快照：记下放了什么、漏了什么、为什么**

- Aster：Context 第 6 步固定持久化 ContextSnapshot，记录来源、预设、请求 digest 与遗漏原因。
- 我们：Context Builder 只在文本里写“另有 N 个更早的动作”（`context/builder.py`），不留可查询的记录。Compaction Probe 答错时，看不出是哪一级压缩丢了什么。
- 建议：每次组装产出一份清单（各区放了哪些条目的引用与 digest、哪些被丢弃及原因、用到 Compaction Ladder 的哪一级），存 ObjectStore，`chat` span 里只带引用。Learning Pipeline 做失败归因时直接可用。
- 挂在：R-03（Context Builder）的后续；S-11 的 Compaction Probe 可以直接读它。设计书附录 G.6 加一段。

### 6.2 第二档

**④ 事件可续读、增量可丢**：Aster 的 In/Out 规定持久事件按 Run 有游标、流式增量可以丢、重连从已提交状态补齐，和 WDP 的做法一致。两份材料都指向同一个缺口，见 [WDP 项目](wdp-project.md) 第 6 节 ②。

**⑤ 同一幂等键配不同正文视为冲突**：我们的 taskstore 按（session_key, client_request_id）去重，不存请求摘要，同一个键带不同的命令会悄悄拿回旧的 task_id。建议存请求正文的摘要，键相同、摘要不同时返回 invalid。挂计划第 7 节“Task Service API 与 Ingress”；设计书 3.1.1 第 1 条可补一句。

**⑥ 检索骨架里的三条顺序规则**：权威复核放在精排之后、去冗余放在复核之后（反过来，复核丢掉的候选会留下补不回的空位）；精排只打分、不增删候选；embedding 身份任一项变化就换 index_version，不混排。我们已有 embedding_version，前两条可以写进 Memory Service 的检索流水线；“可选能力缺失降级并记原因、显式要求的缺失直接报错”与 WDP 的 Rerank 降级是同一条。挂计划第 7 节“Memory Service 检索流水线”。

**⑦ 架构测试强制依赖规则**：Aster 在 CI 里禁止 harness 包 import `os`、`net`、`database/sql`。我们的硬性规则“核心代码只依赖 Provider 接口，不直接引用具体产品的 SDK”目前没有测试守着（`tests/contract` 里没有这类检查）。建议加一条契约测试：扫描 Python 与 Go 的 import，核心包里出现 `qdrant_client`、`psycopg`、供应商 SDK 等就失败（Temporal 工作流代码按规则豁免）。挂 S-02 的后续或 T-14。

**⑧ 判定结果只能收紧或升级**：Aster 把判定分成精确、稳定高频、开放长尾三类，分别交给代码、分类模型、生成模型，并规定模型给出的概率永远不变成授权。我们的 Policy Engine 已经不在 Hook 里做复杂推理（R-01），但这条没有写成显式规则。建议写进设计书附录 F：模型判定可以触发 deny、escalate，不能产生 allow 或通过 gate。Triage 的稳定判定将来可以换成本地分类器，标签从 trace 积累。

### 6.3 第三档：长期或等场景出现

- **凭据在出口代理注入，执行环境永不持有**：比我们“节点代码拿 SecretStore 签发的短期凭据”更严。等 Sandbox 有生产实现时，`web_fetch`、MCP、HTTP 类工具改为由出口代理注入（设计书 4.10、4.12）。
- **自主权靠挣得**：按能力在“影子 → 抽样 → 自动”之间晋级、异常自动回落。可以用在 write_reversible 动作的 gate 上：额度内、审计准确率达标后由策略自动通过。需要先有真实的写场景，再写进 Policy Engine 的设计。
- **HandoffResult 有上限**：子代理返回摘要 ≤ 4 KB、产出、证据、未决问题、副作用。sub_agent 模式的 Node Output 可以参考这个结构。
- **重规划复用已完成节点**：Plan-DAG 重规划时，已完成且输入没变的节点直接复用。我们 replan 后的新 Round 是否复用旧结果，取决于 Planner 怎么读 Task State，值得核对。
- **按失败特征聚类，一次调查**：Learning Pipeline 做失败归因前，可以先按错误签名聚类，每类只归因一次，省成本。
- **代码类工具的加固**：以后加 Git 工具时照 C9 的清单禁用 hooks、过滤器、fsmonitor、LFS smudge、凭据提示；`repo_edit` 可以考虑“先提议、后应用”。

## 7. 不借鉴的

| Aster 的做法 | 不借鉴的原因 |
| --- | --- |
| 自建 Run 引擎、租约、epoch、reconciler（AD-10） | Aster 这样选是为了嵌入式与本地形态不依赖外部服务；我们是服务端，Temporal 已经覆盖，而且有重放测试与故障注入套件 |
| 单一 SQL 事务单元、SQLite 本地形态 | 部署目标不同 |
| Workspace Manager 的四类空间与提交协议 | 我们的 Workspace 是租户内的作用域，不管理文件版本；代码类场景出现前用不上 |
| cell、microVM 预热池、免费版配额 | 规模与产品形态上的问题，超出当前范围；做容量规划时可以回来看 |
| Strategy 的拓扑库 | 我们的 mode、quality、reasoning 已覆盖大部分（见 5.1），Swarm 暂不需要 |
| Care 的平台合同 + 记录桩 | 我们的故障注入套件已有同类做法（`e2e_support/faults.py` 的故障计划与 `ledger.py` 的外部 ledger） |

## 8. 涉及设计书的改动（待人确认）

设计书是导出副本，以下只列建议，由人更新原文档：

1. **3.2.3 或附录 F**：budget_check 改为先预留、后结算，并发 Node 按期望成本预留（对应 ①）。
2. **附录 D.4、D.6**：Node Template 增加 recovery 维度；失败分类增加“结果未知”，只有 queryable 的工具走对账（对应 ②）。
3. **附录 G.6**：Context Builder 每次组装产出可查询的清单，记下遗漏原因（对应 ③）。
4. **3.1.1**：同一 client_request_id 配不同请求正文时拒绝（对应 ⑤）。
5. **附录 I.6**：检索流水线写明复核、去冗余的顺序与降级规则（对应 ⑥）。
6. **附录 F**：模型判定只能收紧或升级，不能产生 allow（对应 ⑧）。
