# 模型预算账本接口切片（2026-11-03）

状态：**测试契约完成；生产接入 DEFER**。

本切片落实人工验收 Q38–Q54 中可以在没有权威共享账本 API 的前提下验证的最小部分。`services/go-model-gateway/internal/budget` 定义 `ReservationProvider`，并提供只用于单测的 `MemoryProvider`。它证明了预留、结算和并发边界，但不替代生产账本。

## 接口契约

- `Reserve(ctx, ReservationRequest)` 在同一个租户的 UTC 日和自然月内，原子检查已用量加 pending reservation；任一启用的上限不足就返回 `providererr.Denied` / `tenant_budget_exceeded`。
- `Settle(ctx, Reservation, actualUSDMicros)` 用实际用量替换 pending reservation，并退回 `reserved - actual`。实际用量必须不大于预留上限；超过上限是调用方违反 upper-bound 契约，返回不可重试的 `settlement_exceeds_reservation`，不静默越过硬上限。
- `tenant_budget_exceeded` 使用现有 `providererr` 的 `Denied` 类别，因此共享重试策略不会重试或切换模型。
- `Limit{Enabled:false}` 表示该周期不限；`Enabled:true, USDMicros:0` 表示显式零预算，保留了“缺省不限”和“零额度拒绝”的区别。

## 为什么只做 fake

现有 Gateway 只有 `MeterRecord` JSON 输出，task-service 只有 Task 级汇总；仓库里没有可供两个入口共同原子预留的权威账本查询/写入 API。把内存 fake 接进生产，或新增一张本地账本表，会形成第二份状态并让 Task admission 与 Gateway 发生漂移。故本切片没有修改 `gateway.Server.Options`，也没有改变现有 MeterRecord 路径。

生产完成的前置条件是由账本负责人提供一个共享实现（HTTP/gRPC/数据库事务均可），其语义至少覆盖：

1. 以租户、UTC 日、自然月为键的已用量和 pending reservation 原子更新；
2. 预留句柄的唯一性及 settle 一次性约束；
3. `tenant_budget_exceeded` 的 `Denied` 映射和客户端不重试语义；
4. 账本不可用时的错误类别、超时和重试边界；
5. Gateway 与 task-service 同时使用该实现，而不是各自缓存或各自计量。

这些条件确认前，MODEL-BUDGET-02 仍是 DEFER；本切片不宣称预算已在生产强制执行。

## 设计决策

| 项目 | 决定 |
| --- | --- |
| Failure | 并发模型调用在“读取已用量→调用→回写”之间竞态，可能越过日/月上限。 |
| Evidence | Gateway 当前只输出 MeterRecord；task-service 的汇总是 Task 范围，缺少共享原子预留接口。 |
| Minimum fix | 一个窄的 reserve/settle provider contract；用内存 fake 覆盖边界，不增加生产表。 |
| Benefit | 可先锁定 Q47–Q50 的错误和结算语义，后续接入唯一账本时有可运行的契约测试。 |
| Performance cost | 生产实现预计每次新模型调用一次账本预留、一次结算；本切片 fake 仅在测试中运行。 |
| Implementation cost | 一个内部 Go 包和并发/边界单测；不改 Gateway 请求路径。 |
| Operational cost | fake 无运维成本；生产账本的容量、超时和告警留在后续接入工作项。 |
| New failure surface | 预留与 settlement 可能失败或超时；生产处理策略需由账本 API 合同确定。 |
| Release slice | MODEL-BUDGET-02 的“接口与 fake 契约”子切片；生产 wiring 仍 DEFER。 |
| Decision | **KEEP（测试契约）；DEFER（生产实现）**。 |

## 验证边界

已检查 `git diff --check`。当前执行环境没有 Go 工具链，因此 `go test ./services/go-model-gateway/internal/budget` 尚未编译或运行；这不构成生产验收结论。

