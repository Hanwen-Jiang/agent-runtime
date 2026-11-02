# 人工验收后的实现工作项（2026-11-03）

人工验收基线见 [manual-acceptance-baseline-2026-11-03.md](manual-acceptance-baseline-2026-11-03.md)。契约和设计已经批准；实现仍按工作项逐个验收。没有满足前置条件的工作项保持 `DEFER`，不以猜测补字段或新建并行状态。

| 工作项 | Failure / Evidence | Minimum fix | 成本与新失败面 | 当前决定 |
| --- | --- | --- | --- | --- |
| MODEL-BUDGET-01 策略字段与默认额度 | 免费租户缺少日/月边界；Q38–54 已批准 UTC、自然月、$3/$20 和未知字段拒绝。 | 增加 `model_budget.daily_usd/monthly_usd`、默认 seed，并严格解析租户配置。 | 只增加配置校验和部署数据；不增加运行时状态。 | **完成**（`ad0aa6a`、`61c8765`） |
| MODEL-BUDGET-02 预算执行 | 并发模型调用可能共同越过上限；现有 Gateway 只有 MeterRecord 输出，Task State 只有单 Task 汇总，没有可原子预留的共享用量 API。 | 复用一个已确认的权威用量记录接口，实现预留→实际结算→退回，并让 Task admission 与 Gateway 双检查。 | 会增加一次账本读写和争用；不能通过内存表或第二套数据库绕过权威来源。 | **DEFER**，待权威账本接口确定 |
| ROUTING-01 多因素选档 | 只按价格会偏好老模型；Q78 要求按租户策略、工作种类/节点阶段能力、发布时间、价格性价比、可用性综合选择。现有 Profile 只有 `freshness.price_at`，没有发布时间、阶段质量或可用性字段。 | 先冻结 `released_at`、task family/node role 质量、availability/reliability 元数据和可解释权重，再改 Profile 校验与 Router 排序。 | 配置字段和权重会改变选档；误把 `price_at` 当发布日期会产生错误路由。 | **DEFER**，缺元数据和权重定义 |
| CONTEXT-01 Activity ContextBuilder | 当前模型节点输入没有完整 Task State、历史和 Builder 依赖；直接接入会改变 Activity payload 与 replay 面。Q87 已明确 Builder 在 Activity 中运行。 | 固定稳定可序列化 Activity 输入，把 Task State/系统约束/历史按 Q89–93 传入，再逐项验收 replay。 | 增加 Activity 输入体积和版本兼容面；不得在 Workflow 中读取外部状态。 | **DEFER**，待输入 payload 与 pinned 版本切片 |
| ENV-01 Runtime EnvRecord | Q99–105 要求 Runtime 是唯一权威，`ready` 还必须有健康确认，`reused` 要绑定 Session/Workspace，`expiry` 必须来自 ledger。现有 `sandbox_envs` 没有健康确认字段，Go Workspace API 只有工作区摘要。 | 复用现有环境记录，增加只读查询 API 和明确 health-confirmed 语义，再接 coding-agent `/env`。 | 需要 proto/API、生成代码和真实桥接验证；不能从 Pod、日志或 `state=ready` 猜状态。 | **DEFER**，等待 Go/buf 与 API 工作项验收 |
| ENV-02 云环境 CRUD | Q16–37 已批准环境与 repo 解耦、`.env`、setup/start 门禁、SecretStore 引用和代理 Header 规则。 | 复用 EnvRecord 生命周期实现 CRUD、Setup 缓存键（脚本内容+基础镜像）、失败门禁和外部密钥引用。 | 需要 Runtime 与 coding-agent 跨仓库协同；OIDC/VPN/私有 DNS/私有 Registry 首期暂缓。 | **DEFER**，依赖 ENV-01 |
| REAL-01 真实验收 | Q94–98 要求 Go、buf、Helm 和真实 Temporal/Postgres/Qdrant/Milvus、Kubernetes、Kata/Firecracker、R2/Cloudflare 全部记录版本与命令。 | 按工具链→基础组件→Sandbox→存储/网络→观测执行，缺资源标记 `blocked_external`。 | 依赖外部账户、节点和凭据；本地静态测试不能替代线上通过。 | **blocked_external** |

MODEL-BUDGET-02 的接口契约子切片已落在 `services/go-model-gateway/internal/budget`：`ReservationProvider` 和仅用于测试的 `MemoryProvider` 覆盖原子预留、实际结算退回、UTC 日/月边界，以及 `tenant_budget_exceeded` 不重试。由于共享权威账本 API 仍未确定，生产 wiring 继续保持 **DEFER**；详见 [model-budget-ledger-interface-2026-11-03.md](model-budget-ledger-interface-2026-11-03.md)。

## 已确认的实现边界

- 默认免费白名单和云上 ConfigMap 是第一批上线收尾内容，外部 ConfigMap 优先。
- 配置文档拒绝未知字段；线上 proto、事件和客户端忽略未知字段。
- 预算拒绝不重试、不切换便宜模型；已经开始的调用按实际用量收尾。
- Kata、gVisor、Firecracker 都是可选 Sandbox 后端；隔离更弱的后端不能静默降级。
- `/env` 是只读投影；查询不到 Runtime 状态展示 `blocked_external` 或 `unknown`。
