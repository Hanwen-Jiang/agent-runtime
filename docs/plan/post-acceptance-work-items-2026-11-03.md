# 人工验收后的实现工作项（2026-11-03）

人工验收基线见 [manual-acceptance-baseline-2026-11-03.md](manual-acceptance-baseline-2026-11-03.md)。契约和设计已经批准；实现仍按工作项逐个验收。没有满足前置条件的工作项保持 `DEFER`，不以猜测补字段或新建并行状态。

| 工作项 | Failure / Evidence | Minimum fix | 成本与新失败面 | 当前决定 |
| --- | --- | --- | --- | --- |
| MODEL-BUDGET-01 策略字段与默认额度 | 免费租户缺少日/月边界；Q38–54 已批准 UTC、自然月、$3/$20 和未知字段拒绝。 | 增加 `model_budget.daily_usd/monthly_usd`、默认 seed，并严格解析租户配置。 | 只增加配置校验和部署数据；不增加运行时状态。 | **完成**（`ad0aa6a`、`61c8765`） |
| MODEL-BUDGET-02 预算执行 | 并发调用需要共享原子预留；原 stdout 无法提供该事实。 | 现有业务库 model_usage + 共享 Policy；Task 准入和五种 Gateway 调用接线，实际结算、退款、取消费用和非重试错误。 | 每次尝试预留/结算事务，未知 usage 保留待账单确认；无第二账本或后台扫描。 | **实现完成**；SQLite、真实 PostgreSQL、Gateway/Task、Helm 通过，线上验收待执行。见 [接线](model-budget-enforcement-2026-11-03.md) |
| ROUTING-01 多因素选档 | Q78 要求按工作种类/节点阶段、发布时间、性价比和可用性选档。 | 已实现严格 Profile 元数据、固定权重、task_family 传参，回填 11 个真实发布日期。 | 无新状态或网络探测；缺实测能力/可靠性分数明确使用默认值，不伪造。 | **已推送 8d49b3b**；代码和359项契约/同步测试通过，真实评分矩阵及线上验收待补。见 [实施记录](model-routing-implementation-2026-11-03.md) |
| CONTEXT-01 Activity ContextBuilder | 旧模型节点 Activity 只有 Node JSON，Builder 无法获得稳定的 Task State、Plan 和历史快照；Workflow 内构建又会破坏 Q87 replay 边界。 | `NodeInput` 携带 JSON 可序列化的 `task_state`、`task_plan`、`history`，StageRunner 在提交 Activity 时深拷贝；NodeExecutor 在 Activity 内调用 ContextBuilder，保留旧输入的兼容路径。 | Activity payload 变大，Builder 摘要/Probe 可能增加模型调用成本；旧历史不带快照时不启用新路径，避免改变重放结果。 | **完成最小切片**（ContextBuilder 接入与正例测试）；Temporal replay/真实 Activity 仍需外部工具验收 |
| ENV-01 Runtime EnvRecord | Q99–105 要求 Runtime 是唯一权威，`ready` 还必须有健康确认，`reused` 要绑定 Session/Workspace，`expiry` 必须来自 ledger。现有 `sandbox_envs` 没有健康确认字段，Go Workspace API 只有工作区摘要。 | 复用现有环境记录，增加只读查询 API 和明确 health-confirmed 语义，再接 coding-agent `/env`。 | 需要 proto/API、生成代码和真实桥接验证；不能从 Pod、日志或 `state=ready` 猜状态。 | **DEFER**，等待 Go/buf 与 API 工作项验收 |
| ENV-02 云环境 CRUD | Q16–37 已批准环境与 repo 解耦、`.env`、setup/start 门禁、SecretStore 引用和代理 Header 规则。 | 复用 EnvRecord 生命周期实现 CRUD、Setup 缓存键（脚本内容+基础镜像）、失败门禁和外部密钥引用。 | 需要 Runtime 与 coding-agent 跨仓库协同；OIDC/VPN/私有 DNS/私有 Registry 首期暂缓。 | **DEFER**，依赖 ENV-01 |
| N-18 文件交付 | 大文件不能直接塞进 Task Event；下载必须绑定租户和 Task，不能让短期 URL 变成跨任务读取入口。 | `FileDelivery.publish/authorize` 复用现有 ObjectStore 与短期 URL，元数据保留 task/file 绑定；过期由 ObjectStore 生命周期负责。 | 每次下载授权增加一次对象 head；公开下载 RPC、Ingress 和浏览器验收仍需契约/真实资源。 | **完成内部 helper**（`e3096a8`）；公开入口 DEFER |
| REAL-01 真实验收 | Q94–98 要求 Go、buf、Helm 和真实 Temporal/Postgres/Qdrant/Milvus、Kubernetes、Kata/Firecracker、R2/Cloudflare 全部记录版本与命令。 | 按工具链→基础组件→Sandbox→存储/网络→观测执行，缺资源标记 `blocked_external`。 | 依赖外部账户、节点和凭据；本地静态测试不能替代线上通过。 | **blocked_external** |

MODEL-BUDGET-02 已由 `libs/go-core/modelbudget` 共享持久实现取代旧 fake；两个服务连接同一业务库，实际费用超过预留也必须记账，后续调用受实际总额限制。旧接口文档仅保留为历史记录，不再表示生产接入待定。

## 已确认的实现边界

- 默认免费白名单和云上 ConfigMap 是第一批上线收尾内容，外部 ConfigMap 优先。
- 配置文档拒绝未知字段；线上 proto、事件和客户端忽略未知字段。
- 预算拒绝不重试、不切换便宜模型；已经开始的调用按实际用量收尾。
- Kata、gVisor、Firecracker 都是可选 Sandbox 后端；隔离更弱的后端不能静默降级。
- `/env` 是只读投影；查询不到 Runtime 状态展示 `blocked_external` 或 `unknown`。
- coding-agent 的环境配置 CRUD 已在其仓库 `384ea04` 推送；Runtime EnvRecord、ready/reused/expiry 投影仍保持 DEFER。
