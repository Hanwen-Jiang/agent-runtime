# 租户模型预算契约前置基线（S-01）

> 状态：候选基线；未修改 `/proto`、`/schemas` 或生成代码。
> 目标：把开放注册的日/月模型预算拆成可审查的验收项，待 S-01 人审后再实现。

## 首版候选行为

- 租户策略新增 `model_budget.daily_usd` 与 `model_budget.monthly_usd`，缺省表示沿用平台策略；非负数，月上限不能低于日上限的产品默认建议不在 schema 强制。
- Model Gateway 以已落库的模型调用计量为唯一使用量来源，按 UTC 日和自然月累计；并发请求在同一租户内必须原子预留或在网关内串行核对，不能因竞态超出上限。
- 预算达到上限时，新模型调用返回 `tenant_budget_exceeded`；已经开始的调用按现有取消/完成语义收尾，不回滚已产生的费用。
- task-service 在新 Task 准入时可读取同一份租户预算快照并拒绝新 Task；网关仍必须二次检查，避免绕过准入直接调用。
- 错误、事件、日志和响应只带租户、周期、limit、used、remaining 等非敏感计量，不带供应商密钥。
- 日界线按 UTC `00:00:00`，月界线按 UTC 当月第一天；边界测试固定时钟，不依赖本机时区。

## 必须先审的问题

1. 预算字段放在 `tenant_policy.schema.json` 还是现有 Registry/Task policy contract；字段编号和默认值如何兼容旧客户端。
2. 计量的权威存储和原子预留位置；禁止在 Task State、Worker 内存或另一个未定义的后台表里复制第二份账本。
3. `tenant_budget_exceeded` 的 proto 错误映射、HTTP 状态和重试语义；达到上限的请求不得自动重试。
4. Task 准入查询超时或计量源不可用时的策略（建议 `transient`，不在本基线擅自决定 fail-open/fail-closed）。
5. 供应商返回最终 usage 晚于请求结束时，预留金额如何结算，超额差额如何记账。

## 复杂度决策

| 项目 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 租户日/月预算 | 单个账号通过大量 Task 累积失控费用 | `open-signup.md` 明确只有 Task 级预算 | 在现有模型计量路径增加周期聚合与单次拒绝 | 限制公开注册的费用上界 | 每次模型调用一次计量核对/预留 | 中高，涉及 schema、网关和准入 | 需要周期结算、告警和人工调额 | 预留与最终 usage 不一致 | S-01/S-02 后 P0-C | KEEP |
| 第二份 Worker 账本 | 多处计量发生漂移 | 当前 Task State 只保存 Task 级预算 | 复用网关权威计量，Worker 只传 tenant/task 上下文 | 避免双写和不一致 | 无额外持久化 | 低 | 只维护一个计量源 | 计量源不可用影响调用 | P0-C | REJECT |
| 全局后台扫描 | 周期结算漏记 | 当前没有证据表明需要扫描修复 | 事件驱动写入，按调用/结算更新周期累计 | 低延迟、低运维 | 每次调用写入 | 中 | 需要周期归档 | 单次写入失败 | P0-C | DEFER |
