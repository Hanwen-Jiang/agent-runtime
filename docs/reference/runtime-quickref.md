# 运行核心速查

> 来源：`精简版` 第 1–7 节。已按设计书附录 A 改用新术语和新的接口名。
> 设计以[设计书](../design/agent-runtime-design.md)第 2–4 章为准。本文把实现时最常查的几张表集中在一处：对象存放在哪、每个模块依赖谁、什么自研什么采用、每个接口的开发实现，以及调研文档旧叫法的对照。

## 1. 对象与存放位置

```text
Tenant → User → Workspace → Session → Task → Round → Stage → Node → Attempt
```

| 对象 | 是什么 | 存放在哪 |
| --- | --- | --- |
| Session | 一段会话：上下文分区、偏好快照 | Temporal 中长期运行的 Session 工作流；元数据在 RelationalStore |
| Task | 用户的一次命令及其完整执行，附带 Task State | Session 工作流启动的 Task 子工作流；Task State 在工作流内，快照在 RelationalStore |
| Round、Stage | 一轮规划出的 Node DAG | Task 工作流的事件历史 |
| Node、Attempt | 一个 Node 的一次执行：输入、输出、成本 | activity 结果记入事件历史；大输出放 ObjectStore，历史里只存引用 |
| Memory Item | 跨 Task 的偏好、经验、事实 | 元数据、版本与谱系在 RelationalStore；向量在 VectorStore |

依据：设计书 2.4（RelationalStore 存 Task State 快照与记忆元数据）、3.1、3.4、附录 K.1。

## 2. 模块速查

| 模块 | 部署单元 | 关键设计 | 依赖 |
| --- | --- | --- | --- |
| Task Service | Control Plane（Go） | Session 与 Task 映射为工作流；会写 Session 状态的 Task 串行，只读 Task 并行；按租户配额准入；提交后立即返回 task_id 与 SSE 订阅地址 | DurableEngine、RelationalStore、Cache |
| Registry | Control Plane（Go） | Task 启动时把用到的模板版本写入工作流输入，运行中不受新发布影响；新版本先影子运行，达标后再切换 | RelationalStore |
| Planner | Worker（Python） | fast 与 light 时退化为规则，不调用模型；Atomic Node 只能从 Node Catalog 实例化，其余写成 Goal Node；每轮只规划到下一个信息检查点 | Model Gateway、Memory Service、Registry |
| Stage Runner | Worker（Python） | 无依赖的 Node 并发启动，结果按 Node id 合并；按 effect 与租户策略自动插入 Hook；把 quality 展开为子图；Goal Node 按 mode 展开 | Node Executor、Policy Engine |
| Node Executor | Worker（Python） | executor、effect、role 三个字段描述 Node；输出分 result、rationale、meta；model 节点一律经 Model Gateway；program 节点在 Sandbox 中执行，工具经 ToolDriver；human 节点挂起等待信号；写操作必须带幂等键；外部调用用 SecretStore 按租户签发的短期凭据 | Model Gateway、Sandbox、ToolDriver、HumanChannel、SecretStore |
| Reviewer | Worker（Python） | 八选一给出 Verdict；判定依据是硬信号加 Progress Check；只看结果与验收条件，不看规划过程；经验与 Recipe 统计异步写回 | Model Gateway、Memory Service |
| Context Builder | Worker 内的库 | 五区布局保住提示缓存的前缀；五级压缩阶梯；精确状态由 Task State 注入。做成库是因为它在每次模型调用的关键路径上，独立部署会多一跳网络延迟 | — |
| Policy Engine | Worker 内的库 | Hook 由 Recipe 与租户策略声明，不由 Planner 编写；只返回 allow、deny、modify、inject；会驳回或重试的 Hook 都有次数上限 | — |
| Model Gateway | Model Gateway（Go） | 按 Model Profile 与“每次成功的期望成本”选 Tier；按供应商维护并发池与限流，带抖动重试和供应商切换；按租户计量与计费；对上统一结构化输出与流式接口 | ModelProvider、Cache（限流计数）、Registry（Model Profile） |
| Memory Service | Memory Service（Python） | 作用域 × 类型组织；在 Session 中产生，验证后晋升；结构化过滤 + 稠密与稀疏混合召回 + 重排；异步写入，每条带来源、信任分与 lineage；租户隔离在服务端强制 | RelationalStore、VectorStore、Model Gateway |
| Learning Pipeline | Learning Pipeline（Python） | trace 用 OpenTelemetry 采集，语义层送 Langfuse，系统层送 Tempo；评测集从生产 trace 沉淀；路由训练、Recipe 统计与搜索离线运行，产出经 Registry 发布 | EventStream、ObjectStore、Registry |

## 3. 五个部署单元

总图见设计书图 1（2.1 节）。

| 部署单元 | 包含 | 状态 | 扩展方式 |
| --- | --- | --- | --- |
| Control Plane | Task Service（Session 与 Task 的生命周期、准入、信号）；Registry（Node Template、Recipe、Model Profile、租户策略） | 自身无状态，状态在 Temporal 与 Postgres | 多副本水平扩展 |
| Worker | Planner、Stage Runner、Node Executor、Reviewer；进程内的 Context Builder 与 Policy Engine | 无状态 | 按任务队列分池，KEDA 按队列积压扩缩 |
| Model Gateway | Model Profile 缓存、选档、限流、供应商切换、计费 | 近乎无状态，计数器在 Cache | 水平扩展 |
| Memory Service | 作用域存储、混合检索、异步写入流水线、治理 | 有状态，依赖 VectorStore 与 RelationalStore | 读写分离，按租户分区 |
| Learning Pipeline | trace 接收、评测、路由训练、Recipe 统计与搜索 | 批处理 | 按任务调度 |

三条拆分规则：

1. **五个部分从一开始就是独立的可部署单元**：同一个 monorepo，各自构建镜像，在 Kubernetes 上各自扩缩。Control Plane 与 Worker 之间经 Temporal 协作，其余部分之间用 gRPC。
2. **契约分两层**：骨架用 protobuf，载荷用 JSON Schema，作为 protobuf 消息里的字段携带（设计书 4.1）。
3. **Context Builder 与 Policy Engine 做成 Worker 内的库**：两者都在每次调用的关键路径上，不单独部署。

## 4. 并发与延迟规则

Temporal 的概念映射、重放约束与恢复场景，见设计书附录 K。

**并发**

- Task 之间：每个 Task 是独立的工作流，Worker 无状态，水平扩展。
- 同一 Session 内：Session 工作流按顺序处理会写状态的 Task；只读 Task 可以直接并行启动。
- Stage 内：无依赖的 Node 并发执行，扇出数设上限。
- 按资源拆分任务队列：decision（Planner、Reviewer）、model 节点、program 节点、offline 各用独立的队列与 Worker 池，互不挤占。
- 租户之间：准入时按租户配额；Model Gateway 按租户与供应商双重限流；交互式请求优先于批量任务。

**延迟**

- fast 路径绕过模型规划。
- deliver 节点边生成边推流。
- 上下文保持稳定前缀，命中提示缓存。
- 记忆写入、trace、经验提炼全部异步，不占关键路径。
- Planner 规划的同时预取记忆与 Recipe；这些都是只读操作，不影响正确性。

## 5. 自研与采用的边界

原则：**自研决定差异化与可控性的部分；已经被充分解决、自己做又容易出错的基础设施，直接采用。** 采用的部分一律挂在内部接口后面。

| 部分 | 做法 | 理由 |
| --- | --- | --- |
| Task Loop、Stage 编排、Node 契约、Reviewer 判定 | 自研 | 这是核心语义；现成 agent 框架各有自己的状态模型和内置提示，难以满足多租户与审计要求 |
| 持久化执行 | 采用 Temporal | 重放、重试、定时器、信号、版本升级都已成熟；Temporal 已集成 OpenAI Agents SDK 与 Google ADK（[来源](https://temporal.io/blog/replay-2026-product-announcements)） |
| tool_loop、sub_agent 等 Goal Node 的执行 | 可以包装现成 SDK，如 OpenAI Agents SDK、Claude Agent SDK | 只作为 mode 的一种实现，每一步都作为 activity 记录 |
| Model Gateway | 自研 | 路由与限流策略是差异化所在；底下直接用各家供应商 SDK |
| 工具接入 | 自研强类型工具契约；MCP 经边缘网关作为一种 ToolDriver 接入 | 内核不受外部协议演进影响，现有 MCP Server 可以直接复用 |
| 记忆 | 自研作用域、晋升与治理；检索用 Qdrant | 多租户治理是需求核心，检索是通用能力 |
| 上下文组装与压缩 | 自研策略，优先调用供应商原生的压缩与上下文编辑 | 策略因工作模式而异；原生能力能降低成本 |
| 可观测性 | 采用 OpenTelemetry、Langfuse、Tempo | GenAI 语义约定已移到独立仓库，仍处于 Development 阶段（[来源](https://www.truefoundry.com/blog/opentelemetry-genai-semantic-conventions)），span 命名要跟随它的版本调整 |
| Sandbox、入口网关、人工渠道 | 外层，只定义接口 | 见设计书 4.10、4.11、4.14 |

## 6. 外层接口与开发实现

核心只依赖下面这些接口；每个接口都配一个开发与测试用的实现（设计书 4.1）。S-02 的一致性测试要同时跑在这两列实现上。

| 接口 | 核心用它做什么 | 开发与测试实现 | 生产实现 |
| --- | --- | --- | --- |
| DurableEngine | Session 与 Task 的持久化执行、重放、定时器、信号 | Temporal 本地开发服务 | 自建 Temporal 集群，Postgres 存储 |
| RelationalStore | 元数据、Registry、Task State 快照、记忆元数据 | SQLite | Postgres |
| Cache | 限流计数、SSE 推流通道、短期缓存、分布式锁 | 内存 | Valkey |
| EventStream | trace 与离线管道 | 内存队列 | Redpanda |
| ObjectStore | 大工具输出、产物、Temporal 大载荷 | 本地磁盘 | S3 兼容存储 |
| VectorStore | 稠密与稀疏向量混合检索 | 内存索引 | Qdrant |
| Sandbox | 执行 program 节点中的代码 | 本地子进程，仅限开发环境 | WASM、容器、microVM，待接入 |
| ModelProvider | 生成、打分、嵌入 | 模拟模型 | 各供应商 API，由 Model Gateway 适配 |
| ToolDriver | 外部工具调用；内核侧为强类型契约 | 本地函数 | 平台内工具；MCP Gateway |
| HumanChannel | 审批与澄清 | 命令行 | IM、邮件、工单系统，待接入 |
| SecretStore | 基础设施密钥与租户动态凭据 | 本地文件 | 云 KMS + Vault 或 OpenBao |
| Telemetry | trace 与指标 | 控制台输出 | OTel Collector → Langfuse、Tempo |
| Ingress | 认证、租户识别、限流；SSE 与 HTTP POST | 内置 HTTP 服务 | API 网关，待接入 |

- **按能力命名，不按产品命名**：叫 Cache 而不是 Valkey，替换实现时不用改核心代码。
- **租户隔离在接口层强制**：每次调用都带租户上下文，实现方无法绕过。
- **核心对外只有一组 Task API**：提交、取消、发送信号走 HTTP POST，结果与进度走 SSE，另有状态查询。

## 7. 调研文档旧叫法对照

`主文档`、`精简版`、`模式图谱` 写于命名统一之前。读原文时用这张表换算；新代码一律用右列的名称。设计书附录 A 是权威版本。

| 调研文档里的叫法 | 设计书名称 |
| --- | --- |
| Orchestrator（头） | Planner |
| 超步 x、Superstep | Stage |
| Synthesizer（尾） | Reviewer |
| 精确节点 | Atomic Node |
| 抽象任务 | Goal Node |
| runner：subflow、react、subagent、task | mode：template、tool_loop、sub_agent、sub_task |
| quality：refine、sample、search | quality：revise、ensemble、search |
| reasoning：none、cot、ps_plus、self_discover | reasoning：direct、stepwise、plan_first、templated |
| effect：read_external | effect：read |
| 节点输出 answer、reasoning、meta | Node Output 的 result、rationale、meta |
| task 账本 | Task State |
| 进度账本、软信号 | Progress Check |
| 判定：完成、继续、修复、重规划、回滚、转交、升级、终止 | Verdict：done、continue、repair、replan、rollback、reroute、escalate、abort |
| 处置阶梯 L1–L7 | 升级顺序（附录 B.8），对应表见 [Task Loop 的依据](task-loop-evidence.md) 3.3 |
| 预检 P0、P1、P2、P3 | Triage：fast、light、full、replan |
| task card、task_plan | Task Plan |
| flow、flow 模板、flow 配方 | Recipe |
| 节点模板库 | Node Catalog |
| 节点模板 | Node Template |
| 钩子、钩子节点 | Hook；插入的审批节点是 `role = gate` 的节点 |
| 钩子与策略引擎 | Policy Engine |
| 上下文装配 | Context Builder |
| 超步执行器 | Stage Runner |
| 节点执行器 | Node Executor |
| 注册中心 | Registry |
| 控制面 | Control Plane |
| 离线面 | Learning Pipeline |
| 模型画像（库） | Model Profile |
| 档位 | Tier |
| 上下文分区 S0、S1、S2、S3、S4 | system、profile、brief、history、status |
| 压缩阶梯 L0、L1、L2、L3、L4 | offload、mask、prune、summarize、reset |
| 压缩质量探针 | Compaction Probe |
| 晋升 | Promotion |
| Task Journal、Session Store | Temporal 工作流状态与事件历史；快照在 RelationalStore |
| Memory Store | Memory Service（RelationalStore + VectorStore） |
| Durable | DurableEngine |
| CacheQueue | Cache |
| VectorIndex | VectorStore |
| ToolProvider | ToolDriver |
| SecretProvider | SecretStore |
| 运行核心 | Runtime（暂名） |
