# 模型预算执行接线

对应人工验收 Q38–Q54。Task Service 和 Model Gateway 已共用现有业务库中的 modelbudget 账本；默认启用 3 美元/UTC 日和 20 美元/自然月。账本结构、跨周期与并发语义见 [共享账本实现](model-budget-ledger-implementation-2026-11-03.md)。

## 执行行为

- Task Service 对新提交检查共享账本；已受理请求的幂等重试仍返回原 Task。此检查不为整个 Task 虚构费用，每次模型调用仍由 Gateway 原子预留。
- Generate、StreamGenerate、Score、Embed、Classify 的每次供应商尝试均先预留。tenant_budget_exceeded 沿现有 Denied 错误通道返回，不重试、不切换便宜模型。
- 输入按序列化字节和适配器提示开销保守预估；输出按请求上限和 Profile max_output 的较小值预留，并把同一上限发送给供应商。未指定输出上限时使用网关的默认上限：可见输出 16384 个 token（`AR_MODEL_DEFAULT_MAX_OUTPUT`，0 回到 Profile 上限）加上这次的思考预算，不超过 Profile 上限（BUDGET-DEFAULT，2026-11-04：原来用 Profile 上限，kimi-k3.1-pro 的 max_output 是 1,048,576，一次预留 10.49 美元，默认档每天 3 美元的租户第一次调用就被拒）。预估不是实际计费。
- 收到实际 usage 后按服务该次请求的 Profile 单价结算，缓存命中按 cache_read_per_m 计价。已计费的失败、超时和收到 usage 后中断的流也结算。实际费用超过估值仍记账，之后的请求受实际余额约束。
- 客户端取消不取消已知费用的结算；结算使用独立的 5 秒超时。存储故障返回不可重试的 model_usage_unsettled，保留预留，避免重新调用模型产生第二笔费用。
- 断线、超时或成功回答缺 usage 时不伪造零费用，保留预留并记录 reservation_id。已确认的上游权限拒绝/模型不存在可退回零费用。没有引入后台扫描或自动退款；无法取得的供应商费用仍需账单确认。
- 先前仅测试使用的 internal/budget MemoryProvider 已移除；实际账本只有一套。其“超预留费用拒绝入账”语义与 Q43 冲突，已用实际结算测试替换。

## 配置与部署

两个服务必须使用同一个 AR_RELATIONAL_DSN；本地开发使用同一个绝对 AR_DATA_DIR，其 relational 子目录保存账本。dev_stack.py 已统一路径。Helm 已让 Gateway 读取与 Task Service 相同的数据库 Secret，并让两个服务挂载同一份租户策略 ConfigMap。默认 Chart 的独立 emptyDir 仅用于组件试跑，跨 Pod 预算验收使用已配置业务数据库的 cloud/production values。

两个服务使用同一个 Policy 解析器。配置文件 default.model_budget 是部署硬上限，缺省为 3/20；当前 Registry 租户策略与文件 tenants 条目可覆盖但不能超过硬上限，缺项继承。文件中的显式租户字段优先。配置字段拼错、负数、溢出或超平台上限均拒绝。额度调整从下一次 Task 准入/模型预留起生效，不取消正在运行的调用；账本不会另存策略副本。

Gateway 使用 AR_REGISTRY_ADDRESS 读取当前租户策略。生产 Chart 和本地 dev stack 均已接入；Registry 失败按 tenant_policy_unavailable 返回，不绕过检查。历史 Task 的内容/模板版本固定和“新调用使用当前预算”是分别执行的既有要求。

## 复杂度决策

| 项目 | 结论 |
| --- | --- |
| Failure | 两入口各自检查、并发竞态或取消丢 usage，导致额度失效或漏账。 |
| Evidence | 旧 Gateway 仅写 stdout；旧取消分支在读取 usage 前返回；Q41/Q43–51 明确要求双检查和实际结算。 |
| Minimum fix | 复用业务 RelationalStore、一个共享 Policy、现有 Gateway attempt 循环内预留/结算；流适配器只暴露已收到的 usage。 |
| Benefit | 重启、并发和双入口使用同一事实；取消不抹去已知账单。 |
| Performance cost | 每次尝试一次预留事务、一次结算事务、当前策略读取；Task 新提交一次去重查询和一次余额查询。同租户预留短事务串行。 |
| Implementation cost | 现有入口、适配器、启动配置及测试；无新服务或依赖。 |
| Operational cost | 使用已有数据库迁移/权限；极少数 usage 未知的预留需按供应商账单处理。 |
| New failure surface | 数据库/Registry 不可用会拒绝新调用；保守估算可能提前占满额度。 |
| Release slice | 本次完成本地与真实 PostgreSQL 接线验证；线上供应商账单与 k3s 发布仍按逐项验收。 |
| Decision | KEEP 必需执行路径；DEFER 自动对账、聚合余额表和后台重试层。 |

## 验证

Go 1.26.5：共享账本、全部 Gateway 包、OpenAI 兼容适配器、Task API 测试通过；WSL Debian + PostgreSQL 17.10 的账本、Task Store、Registry、Task API 与 task-service 命令测试通过。覆盖并发预留、跨进程/重开、日/月边界、退款、超估实际费用、缓存价格、非重试预算拒绝、已受理任务去重、流中断费用及缺 usage 保留。

Helm 3.19.0：Chart 66 项与共享数据库/策略挂载 4 项测试通过。未把这些本地与临时数据库测试写成线上供应商/k3s 验收完成。
