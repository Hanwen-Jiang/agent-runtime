# 人工验收基线：Cloud Environment / Runtime 设计与契约（2026-11-03）

> 状态：ACCEPTED（契约与设计）。本文件由人工验收问卷与本轮 1～112 项回答共同形成，作为第一阶段冻结基线。实现按工作项逐个验收，不因本文件通过而一次性放行全部实现。
>
> 本文件只记录决策，不修改 proto、schema 或生成代码。任何实现必须先有对应正反例测试，并在工作项验收时单独记录结果。

## 统一审批规则

- 本轮覆盖全部列出的设计域。
- 本轮批准契约和设计；实现按工作项拆分验收。
- 未通过人工评审的功能保持现状，不改 proto/schema。
- 兼容性基线允许新增字段，禁止删除旧字段；字段编号永久保留，删除字段使用 reserved。
- 架构负责人对设计矛盾做最终决定；安全负责人对安全问题拥有否决权。
- 线上 proto、事件和 gRPC/HTTP 客户端忽略未知字段；配置类文档（Model Profile、租户策略）拒绝未知字段，避免拼写错误被解释为零值。
- 未决事项标记 DEFER，不得进入实现；本基线冻结后只接受可追踪变更。

## 验收矩阵

### 一、总体（1～5）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 1 | A：全部覆盖 | 本轮覆盖所有设计域。 | 架构负责人 / 已批准 |
| 2 | A：仅批准契约和设计 | 实现按工作项逐个验收，不一次性放行。 | 架构负责人 / 已批准 |
| 3 | A：保持现状，不改 proto/schema | 未通过项进入 DEFER。 | 架构负责人 / 已批准 |
| 4 | A：允许新增、禁止删除 | 保持向后兼容。 | 协议负责人 / 已批准 |
| 5 | B：架构负责人 | 架构负责人拍板；安全相关问题安全负责人可以否决。 | 架构 + 安全 / 已批准 |

### 二、S-01/S-02：Proto、Schema 与兼容性（6～15）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 6 | A：契约变更必须先有正反例测试 | 测试先于 schema 与实现。 | 协议负责人 / 已批准 |
| 7 | A：字段编号永久保留 | 废弃字段标记 reserved，不得复用。 | 协议负责人 / 已批准 |
| 8 | D：按协议类型区分 | 线上 proto/事件客户端忽略未知字段；配置类文档拒绝未知字段。 | 协议 + 配置负责人 / 已批准 |
| 9 | A：映射为 UNKNOWN/UNSPECIFIED | 旧客户端继续工作。 | 协议负责人 / 已批准 |
| 10 | A：统一定义 gRPC、HTTP、事件映射 | 错误 code 稳定一致。 | 协议负责人 / 已批准 |
| 11 | A：客户端只依赖稳定 code | 不依赖字符串描述。 | 协议负责人 / 已批准 |
| 12 | A：向后兼容优先 | 新增能力不能破坏旧客户端。 | 协议负责人 / 已批准 |
| 13 | A：每次 schema 变更保留 breaking 基线 | CI 与发布前均可复核。 | 协议负责人 / 已批准 |
| 14 | A+B+C+D+E | 新增 RPC、持久化字段、错误码、事件顺序变化、权限语义变化都重新走 S-01。 | 架构 + 协议 / 已批准 |
| 15 | A：生成代码与 schema 同一提交 | 生成结果可追溯。 | 协议负责人 / 已批准 |

### 三、D-23/D-24：云环境配置（16～37）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 16 | A：环境独立保存，启动会话时选择 repo | 云环境与 repo 解耦。 | Runtime EnvironmentService / 已批准 |
| 17 | A：允许跨仓库复用 | 使用者在会话启动时选择 repo。 | Runtime EnvironmentService / 已批准 |
| 18 | A：同一用户下名称唯一 | 不要求平台全局唯一。 | Runtime EnvironmentService / 已批准 |
| 19 | A：环境描述需要长度限制 | 采用 4 KB 上限。 | Runtime EnvironmentService / 已批准 |
| 20 | A：新会话启动、代码执行前 | Setup 在代码执行前运行。 | Runtime EnvironmentService / 已批准 |
| 21 | A：缓存成功结果，失败重新执行 | 缓存键按脚本内容 + 基础镜像哈希命中。 | Runtime / Sandbox / 已批准 |
| 22 | A：Setup 失败禁止启动 | 失败不进入交互运行时。 | Runtime / Sandbox / 已批准 |
| 23 | A：Start 只启动交互运行时 | Start 不负责安装依赖。 | Runtime / Sandbox / 已批准 |
| 24 | A：安装脚本与启动脚本分开 | Setup 与 Start 有独立职责。 | Runtime / Sandbox / 已批准 |
| 25 | A：环境使用者可见 | 普通环境变量禁止存放秘密。 | Runtime / 安全 / 已批准 |
| 26 | A：env，一行一个 KEY=value | 普通环境变量统一使用 env 格式。 | Runtime EnvironmentService / 已批准 |
| 27 | A：密钥始终在环境外 | 禁止直接写入 env。 | SecretStore / 安全 / 已批准 |
| 28 | A：仅向允许的 HTTPS 域名请求时替换 | 启动或普通代码读取时不做全量替换。 | Egress Proxy / 安全 / 已批准 |
| 29 | A：完全开放、受限列表、禁止三档 | 网络访问策略与 Claude 云环境模型一致。 | Egress / 安全 / 已批准 |
| 30 | A：子域名匹配可配置 | 域名匹配语义显式记录。 | Egress / 安全 / 已批准 |
| 31 | A：代理向目标 Host 添加 Header | 模型、任务和运行时都看不到 Token。 | Egress Proxy / 安全 / 已批准 |
| 32 | D：默认 Header 可配置 | 默认 Authorization: Bearer；按域名可配置 Header 名（如 x-api-key）；禁止 Query 参数。 | Egress Proxy / 安全 / 已批准 |
| 33 | A：仅我自己 | 当前 MVP 不做团队或组织共享。 | Runtime EnvironmentService / 已批准 |
| 34 | E：高级网络能力首期全部暂缓 | OIDC、VPN、私有 DNS、私有 Registry 暂不进入首期。 | 架构 + 安全 / DEFER |
| 35 | A：立即标记删除，异步清理 | 状态转换与清理任务沿用现有生命周期。 | Runtime EnvironmentService / 已批准 |
| 36 | A：Runtime EnvironmentService 为权威 | coding-agent、Kubernetes 不维护第二套环境状态。 | Runtime EnvironmentService / 已批准 |
| 37 | A：不允许从日志推断 | 必须查询权威记录。 | Runtime + coding-agent / 已批准 |

### 四、模型日/月预算（38～54）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 38 | A：复用现有用量账本 | 不新建并行预算数据库。 | Model Gateway / Task Service / 已批准 |
| 39 | A：UTC | 日切分统一使用 UTC。 | 账本负责人 / 已批准 |
| 40 | A：自然月 | 月度窗口按 UTC 自然月切分。 | 账本负责人 / 已批准 |
| 41 | A：达到上限即拒绝新请求 | 已运行请求继续完成。 | Model Gateway / 已批准 |
| 42 | A：每日 3 美元、每月 20 美元 | 免费租户默认额度。 | 产品 + 架构 / 已批准 |
| 43 | A：按供应商返回的实际用量计费 | 只结算供应商报告的用量。 | Model Gateway / 已批准 |
| 44 | A：有实际 token 就计费 | 超时不抹掉已产生的用量。 | Model Gateway / 已批准 |
| 45 | A：按已产生的用量计费 | 取消中的流式请求按已产生用量结算。 | Model Gateway / 已批准 |
| 46 | A：按 Profile 缓存价格计费 | 使用 Profile 的缓存单价。 | Model Gateway / 已批准 |
| 47 | B：开始前原子预留，结束后按实际用量结算 | 实际结算后退回预留多出的部分。 | Model Gateway / 账本 / 已批准 |
| 48 | A：原子预留额度 | 并发请求先预留，避免共同越过上限。 | Model Gateway / 账本 / 已批准 |
| 49 | A：tenant_budget_exceeded | gRPC、HTTP、事件使用统一错误映射。 | 协议负责人 / 已批准 |
| 50 | A：不自动重试 | 预算拒绝不切换便宜模型。 | Model Gateway / 已批准 |
| 51 | A：Task 准入和 Gateway 都检查 | 两处检查复用同一权威账本。 | Task Service + Gateway / 已批准 |
| 52 | A：允许覆盖但不得超过平台硬上限 | 租户策略不能突破平台边界。 | Registry / 架构 / 已批准 |
| 53 | A：新 Task/新模型调用生效 | 已有请求不回滚。 | Task Service + Gateway / 已批准 |
| 54 | A：按租户、Task、模型、供应商全部拆分 | 账单和审计维度完整保留。 | 账本负责人 / 已批准 |

### 五、N-04/N-19/N-20：计划、快照和事件（55～62）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 55 | A：保存当前 Task Plan 版本 | Plan 是持久状态。 | Task Loop / 已批准 |
| 56 | A：plan_updated 是可重放事件 | 事件进入单写者历史。 | Task Loop / 已批准 |
| 57 | A：全量快照加增量事件 | 兼顾恢复与可重放。 | Session / Task Loop / 已批准 |
| 58 | A：回退最近可用快照并报告错误 | 不静默从零开始。 | Session / Task Loop / 已批准 |
| 59 | A：请求接收至第一条可展示事件 | 作为首事件延迟口径。 | Observability / 已批准 |
| 60 | A：Task Loop 单写者保证顺序 | 不依赖前端排序。 | Task Loop / 已批准 |
| 61 | A：稳定事件 ID 幂等去重 | 重复事件不改变最终状态。 | Event Store / 已批准 |
| 62 | A：中间增量可丢失 | 最终状态不受影响；最终事件必须可恢复。 | Streaming / Task Loop / 已批准 |

### 六、Firecracker、vsock 与快照（63～69）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 63 | B：允许 Kata/gVisor 替代 | 线上已经使用 Kata 与 gVisor；Firecracker 是可选后端，不要求唯一。 | SandboxBroker / 已批准 |
| 64 | A：标记 blocked_external | 不允许悄悄降级到隔离更弱的后端。 | SandboxBroker / 安全 / 已批准 |
| 65 | A：vsock | Guest 与 Host 通过 vsock 通信。 | SandboxBroker / 已批准 |
| 66 | A：版本固定的 OCI 镜像转换 | Rootfs 来源可复现。 | SandboxBroker / 已批准 |
| 67 | A：不允许跨租户复用 | 快照租户边界固定。 | SandboxBroker / 安全 / 已批准 |
| 68 | A：环境状态记录优先 | 快照不能覆盖权威环境状态。 | Runtime EnvironmentService / 已批准 |
| 69 | A：仅明确 transient 错误重试 | 其他错误直接失败并记录。 | SandboxBroker / 已批准 |

### 七、N-15/N-16/N-18：Skills、MCP 与文件交付（70～77）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 70 | A：Registry 版本化发布 | Skill 不直接读取用户目录。 | Registry / 已批准 |
| 71 | A：用户 Skill 只能扩展 | 不允许覆盖平台 Skill。 | Registry / 安全 / 已批准 |
| 72 | A：SecretStore，Task 只拿引用 | MCP 凭据不进 env 或 proto 明文。 | SecretStore / 安全 / 已批准 |
| 73 | A：Server、Tool、Effect 三层 | 权限按三层共同判定。 | MCP Gateway / 安全 / 已批准 |
| 74 | A：默认继承但不能提升 | 子 Agent 权限上限不超过主任务。 | Task Loop / 安全 / 已批准 |
| 75 | A：ObjectStore + 短期下载链接 | 不通过 SSE 传大文件。 | ObjectStore / 已批准 |
| 76 | A：绑定租户和 Task | 下载链接不能脱离授权范围使用。 | ObjectStore / Ingress / 已批准 |
| 77 | A：自动失效并清理 | 过期链接失效，文件按生命周期清理。 | ObjectStore / 已批准 |

### 八、R-02/R-03/R-04/R-06（78～86）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 78 | A：租户策略、能力、成本、可用性综合选择 | 轻重档不能只看价格；按工作种类和节点阶段细分能力；把发布时间纳入评分，老模型明显降权，提高发布时间负相关权重；同时考虑价格与性价比。 | Model Gateway / 架构 / 已批准 |
| 79 | A：仅 transient 错误切换模型 | 永久错误、策略拒绝、预算拒绝不切换。 | Model Gateway / 已批准 |
| 80 | A：模型请求必须携带租户上下文 | 计量、隔离、审计均依赖租户上下文。 | Model Gateway / 已批准 |
| 81 | A：租户、Task、Session 分层隔离 | 三层边界都必须验证。 | Context / 安全 / 已批准 |
| 82 | A：默认禁止跨 Task 读取 | 显式授权才允许。 | ContextStore / 安全 / 已批准 |
| 83 | A：按大小和敏感级别决定 | 工具输出不默认全部进入模型上下文。 | ContextBuilder / Tool Runtime / 已批准 |
| 84 | A：受控 context_read 工具 | 长内容外置后按授权分段读取。 | ContextBuilder / Worker / 已批准 |
| 85 | A：无法确认秘密时拒绝 | SecretStore 失败不使用旧缓存或空值继续。 | SecretStore / 安全 / 已批准 |
| 86 | A：工具侧先脱敏 | 后续链路只做最低限度检查，不把 UI 脱敏当安全边界。 | Tool Runtime / 安全 / 已批准 |

### 九、R-03 ContextBuilder 与 Temporal Replay（87～93）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 87 | D：Activity 输入进入事件历史 | ContextBuilder 在 Activity 中运行；输入必须稳定、可序列化并作为 Activity 输入进入历史；Workflow 本身不做上下文构建。 | Worker / Temporal / 已批准 |
| 88 | A：禁止读取时间、随机数、外部状态 | 所需输入显式传入 Activity。 | Worker / Temporal / 已批准 |
| 89 | A：先保留系统约束和 Task State，再处理历史 | 外置、压缩先动旧工具结果，再摘要旧消息，最近消息保留。 | ContextBuilder / 已批准 |
| 90 | A：精确 Task State 优先 | 系统约束与精确状态固定不动。 | ContextBuilder / 已批准 |
| 91 | A：工具定义计入上下文预算 | 预算计算覆盖工具定义。 | ContextBuilder / 已批准 |
| 92 | A：仅持久化可重放所需结果 | 不把所有中间构建细节变成持久状态。 | ContextBuilder / 已批准 |
| 93 | A：历史 Task 使用 pinned 版本 | ContextBuilder 版本升级不重写历史任务。 | Worker / Temporal / 已批准 |

### 十、真实验收环境（94～98）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 94 | A：Go、buf、Helm 是本地契约验收必需工具 | 缺失时不能宣称验收通过。 | Release Engineering / 已批准 |
| 95 | A：标记 blocked_external | 缺少真实 Temporal/Postgres/Qdrant 等资源不能标记 passed。 | Release Engineering / 已批准 |
| 96 | G：全部组件 | 首个真实集群验收包含 Temporal、PostgreSQL、Qdrant/Milvus、Docker/Kind/Kubernetes、Kata/Firecracker、R2/Cloudflare。 | Release + Infra / 已批准 |
| 97 | A：必须记录版本和命令 | 结果可复现。 | Release Engineering / 已批准 |
| 98 | A：允许继续合并纯代码 | 必须显式标记未完成 gate。 | Release Engineering / 已批准 |

### 十一、coding-agent 与 /env 状态投影（99～105）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 99 | A：Runtime 提供权威 EnvRecord | coding-agent 不维护第二套环境状态。 | Runtime EnvironmentService / 已批准 |
| 100 | A：状态、时间戳、租户、Session、后端 ID | ledger 查询接口返回这些字段。 | Runtime EnvironmentService / 已批准 |
| 101 | A：Runtime 记录为 ready 且健康检查通过 | Pod 存在或命令成功不足以判定 ready。 | Runtime EnvironmentService / 已批准 |
| 102 | A：同一 Session/Workspace 复用既有环境 | 不能只按环境 ID 或 Pod 名判断 reused。 | Runtime EnvironmentService / 已批准 |
| 103 | A：Runtime ledger 的明确过期时间 | 不从最后活动时间或 Kubernetes TTL 推断。 | Runtime EnvironmentService / 已批准 |
| 104 | A：显示 blocked_external 或 unknown | 查询不到 Runtime 状态时不猜测 ready/reused。 | coding-agent / Runtime / 已批准 |
| 105 | A：只读投影 | /env 不直接修改环境状态。 | coding-agent / 已批准 |

### 十二、发布顺序与决策结果（106～112）

| 编号 | 决策 | 补充语义 | 责任 / 状态 |
| --- | --- | --- | --- |
| 106 | A：默认模型白名单和云上 ConfigMap | 已在最新提交中上线；内置策略同时包含免费模型白名单和 $3/$20 默认额度，外部 ConfigMap 优先。 | Model Gateway + Helm / 静态验收完成，真实 Helm render 待工具 |
| 107 | A：schema、账本、时区、计费规则全部批准后实现 | 已批准，本基线冻结后进入独立实现批次。 | Model Gateway + Task Service / 待实现 |
| 108 | A：ContextBuilder 输入、截断顺序、replay 语义冻结后实现 | 已批准；模型节点接入仍单独验收。 | Worker / Temporal / 待实现 |
| 109 | A：工具链 → 基础组件 → Sandbox → 存储/网络 → 观测 | 真实环境按此顺序推进。 | Release + Infra / 已批准 |
| 110 | A：每题形成 decision、owner、截止日期 | 本文件记录 decision 与 owner；截止日期在工作项看板维护。 | 架构负责人 / 已批准 |
| 111 | A：无法达成一致标记 DEFER | 不由实现人员自行选择更复杂方案。 | 架构负责人 / 已批准 |
| 112 | A：冻结第一阶段基线 | 后续只接受可追踪变更。 | 架构 + 安全 / 已批准 |

## 第一阶段发布切片

1. 已发布：默认免费模型白名单、云上内置 ConfigMap、外部 ConfigMap 优先覆盖。
2. 下一批契约实现：环境 CRUD、repo 解耦、env、Setup/Start 门禁、SecretStore 引用和 Runtime EnvRecord。
3. 预算批次：复用现有用量账本，UTC 自然日/月，原子预留与实际结算，Task 准入和 Gateway 双重检查。
4. R-03 批次：先冻结 Activity 输入与 replay 语义，再接入模型节点 ContextBuilder。
5. 真实环境：严格按 Q109 顺序执行；缺少组件按 blocked_external 记录。

## 基线变更规则

- 任何 proto/schema、事件顺序、权限语义、持久化字段、错误码或网络密钥行为变更，重新走 S-01。
- 任何安全问题可由安全负责人否决并将工作项退回 DEFER。
- 该基线不授权跳过工作项验收，也不授权通过猜测补齐 Runtime 状态或真实基础设施结果。

