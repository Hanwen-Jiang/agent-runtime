# R-02 / R-03 成本边界与上下文取回契约基线（候选）

> 状态：契约与设计已由人工验收基线批准；成本账本接入、模型节点 ContextBuilder 接入和路由元数据按独立工作项验收。
> 本文把现有代码事实和已冻结语义分开；未具备权威账本、路由元数据或 Temporal Activity 输入契约的切片继续标记 DEFER。

## 当前证据

- Model Profile 已声明输入、输出和缓存读取单价，并由 Model Gateway 计算一次调用的 `cost_usd_micros`；Worker 目前还同时维护 Task/Stage/Node 级费用，因此“权威计量、预留和最终结算”必须先定口径。
- `CallContext.max_cost_usd_micros` 已出现在调用上下文，但不能把一次价格比较误当成完整硬限制；失败重试、供应商无价、工具调用和缓存 token 的计费语义仍需冻结。
- Context Builder 已为 tool loop 组装上下文；模型节点、外置对象和 `context_read` 的组合边界需要明确，避免同一内容被重复注入或未经授权取回。
- ObjectStore / Claim-Check 已按租户作用域提供 `obj://` 引用、大小与缺失错误；R-03 需要约束模型节点可读前缀、最大读取量、类型和脱敏行为，不能把通用 ObjectStore 直接暴露成任意浏览器。

## R-02：模型调用成本硬限制

首版候选规则：

1. 费用只由已冻结的 Model Profile 价格与最终 usage 计算；输入、输出、缓存读取 token 分别计价，未定义的价格不得静默按零计。
2. 一次调用在发送供应商请求前做租户/Task 额度检查；并发调用必须原子预留，不能让两个请求同时通过同一剩余额度。
3. 供应商失败只对已经产生的可计费用量结算；`tenant_budget_exceeded`、未知价格和策略拒绝不得自动重试。
4. 已开始的调用按现有取消/完成语义收尾；最终 usage 高于预留时补记差额并返回有限的非敏感计量，不能回滚已产生费用。
5. Task 准入检查与 Gateway 检查共用同一权威计量事实；Worker 的 Task State 只保留任务可见摘要，不创建第二个租户周期计量源。

## R-03：模型节点 Context Builder 与 `obj://` 取回

首版候选规则：

1. 模型节点通过 Context Builder 获取系统约束、任务输入、已授权 memory、工具说明和显式外置对象；来源顺序、每段预算和截断规则固定且可重放。
2. tool loop 与普通 model node 共用同一构建器结果，不重复拼接系统提示、工具 schema 或对象内容；旧请求没有新字段时继续使用现有最小上下文。
3. `obj://<tenant>/<key>` 只允许当前 tenant 且只允许已登记的 context/object 前缀；对象缺失、过期、越权、类型不允许或超出读取预算时返回有限错误，不回退到其他租户或宿主路径。
4. 取回按最大字节/ token 和允许的内容类型执行；长对象必须分段或摘要，原始 secret、代理凭据和未授权工具输出不进入 prompt、事件、日志或 trace。
5. ObjectStore 临时不可用与对象不存在保持不同错误语义；实现复用现有 ObjectStore/Claim-Check 生命周期，不新增上下文缓存表、后台扫描或第二个状态机。

## 正反验收范围

契约 fixture 位于 `tests/contract/fixtures/r02_r03_cost_context_contract_cases.json`，测试锚点位于 `tests/contract/test_r02_r03_cost_context_contract_cases.py`，覆盖：

- 输入/输出/缓存/工具成本、未知价格、并发预留、租户隔离、在途结算和拒绝不重试；
- model node 与 tool loop 的 Context Builder 复用、来源授权、预算截断、旧客户端兼容；
- `obj://` tenant/prefix 授权、大小/类型/过期/缺失、脱敏和对象服务暂时不可用；
- 不新增第二个周期计量源、缓存状态机或后台修复扫描。

## 复杂度决策

| 机制 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 单次成本预留 | 并发请求共同穿透剩余额度 | R-02 要求硬上限，当前只存在调用后 usage | 在现有权威计量路径增加一次原子预留/释放 | 额度上界可证明 | 每次调用一次条件写入 | 中高，需 Gateway/准入共用接口 | 需处理超额结算和过期预留 | 预留与最终 usage 不一致 | S-01 后 R-02 | KEEP |
| 未知价格拒绝/人工策略 | 供应商价格缺失却按零放行 | Profile freshness 已是必填，但跨供应商语义未定 | 明确 `unknown_price` 的拒绝或 review 结果 | 避免费用失真 | 一次 Profile 查找 | 中 | 价格更新和告警 | 新模型无法调用 | R-02 契约评审 | DEFER 直到策略确认 |
| Context Builder 单一组装 | tool loop 与 model node 重复注入或漏掉约束 | 当前两条调用路径并存 | 复用现有 builder 的不可变结果 | 提示一致、可重放 | 一次组装和预算裁剪 | 中 | 需维护来源元数据 | 旧节点字段缺失 | S-02/R-03 | KEEP |
| `obj://` 受限取回 | 模型读取别的租户或任意对象 | ObjectStore 已有 tenant scope；context_read 已存在 | 绑定 tenant + 允许前缀 + size/type 上限 | 降低数据泄露面 | 一次授权检查和读取 | 低中 | 需设置保留/过期规则 | 配置前缀误配导致拒绝 | R-03 首版 | KEEP |
| 第二份计量/上下文状态 | 账目或 prompt 来源漂移 | 已有 Gateway usage、Task State、ObjectStore | 复用现有权威事实，只返回 ready/denied/unknown | 降低运维与迁移成本 | 无新增后台工作 | 低 | 无新表和对账循环 | 既有事实不足时需补接口 | R-02/R-03 | REJECT |

## 进入实现前的门槛

1. 人工确定成本权威源、预留/结算和未知价格结果，并为 Gateway 与 Task 准入指定同一接口。
2. 确定 Context Builder 的来源优先级、预算单位、截断和 tool loop 复用方式。
3. 确定 `obj://` 允许前缀、最大读取量、类型 allowlist、过期和错误映射。
4. 在上述决定后再修改 proto/schema 或业务实现；当前 fixture 只冻结审查输入，不宣称 R-02/R-03 已完成。
