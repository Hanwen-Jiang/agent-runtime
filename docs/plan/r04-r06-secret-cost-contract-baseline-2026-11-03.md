# R-04 / R-06 SecretStore 与成本发布契约前置基线

> 状态：候选基线，未修改 `/proto`、`/schemas`、生成代码或运行时代码。
> 日期：2026-11-03。本文件把生产 SecretStore、成本核算和 Registry/Learning 发布边界拆成可审查的正反例；通过 S-01/S-02 人工评审后再进入实现。

## 当前事实

- `SecretStore` 已是跨语言 Provider 抽象；本地文件实现用于开发和测试，正式部署仍需要 OpenBao/Vault 或云 KMS 适配、身份绑定和轮换运维。[云部署计划](cloud-deploy.md) 当前把正式 SecretStore 标为后续切片。
- Worker 的工具凭据应是短期、按用途签发并在调用后撤销；事件、trace、工作流载荷和 ObjectStore 结果不能携带原值。Collector 已有脱敏边界，但它不能替代应用侧在写入前的最小化处理。
- Model Gateway 是模型调用的唯一出口，模型 Profile 已带 `price_at`；成本语义需要固定输入、输出、缓存和供应商失败的结算规则。Task State 只能保存任务摘要，不能另建一份独立的权威计量来源。
- Registry 采用追加版本与 Task 启动时固定版本；Learning Pipeline 的候选先经过评测，再经 Publish/Promote 进入 Registry，不能直接改正在运行的配置。真正的生产 SecretStore、版本回滚和跨租户成本对账仍需真实服务验收。

## R-04 首版边界

1. **生产 Provider**：只提供受控的 `read`、短期 `issue`、`revoke` 和按引用的元数据查询；调用方按 `tenant + purpose + subject` 绑定，拒绝跨租户、跨用途或未授权读取。原值不得进入错误、日志、trace、workflow payload、事件或 API 响应。
2. **工具输出**：应用侧在事件/trace/ObjectStore 写入前执行统一字段与内容脱敏；脱敏失败阻止写入并返回有限错误分类。用户确实需要的非敏感输出仍可保留，不能用全量丢弃代替规则。
3. **批量撤销与轮换**：按租户、用途或凭据引用选择范围；重复请求幂等，旧凭据不再签发给新调用，进行中的调用按既有取消/完成语义收尾。轮换只替换引用指向，不复制业务状态。
4. **失败语义**：SecretStore 不可用、引用缺失、过期或被撤销时，安全敏感操作拒绝；只读诊断可返回 `unavailable`/`not_found` 等有限分类。任何错误都不包含密钥原值。

## R-06 首版边界

1. **成本权威来源**：Model Gateway 在一次调用完成或明确失败时写入唯一成本事件；输入、输出、缓存读取、工具/供应商附加费用按冻结的 Profile 价格版本计算。未知价格或不完整 usage 不得静默按零计费。
2. **并发与租户隔离**：预留、结算和失败释放在同一租户范围内原子处理；一个租户的价格、余额或失败不影响另一租户。Task State 只引用调用摘要，不成为第二个计量来源。
3. **版本与回滚**：Registry 已发布版本不可变；Task 固定的行为版本继续可读，价格和安全策略按既定实时规则处理。Canary/Promote 只能追加新版本并记录评测证据；回滚选择前一个已发布版本，不覆盖旧内容。
4. **Learning 发布门禁**：候选必须有评测集版本、样本数、阈值和成本证据；门禁失败不得调用写入 Registry 的方法。Publish 后保持 shadow，只有 Promote 成功才改变默认版本；Promote 失败时默认版本保持不变。
5. **未知与故障**：价格缺失、供应商 usage 延迟、Registry 不可用、重复 Publish/Promote、证据版本不匹配都返回可分类结果，不能靠自动重试制造第二笔成本或第二个发布状态。

## 验收清单

正反例见 `tests/contract/fixtures/r04_r06_secret_cost_contract_cases.json`，结构锚点见 `tests/contract/test_r04_r06_secret_cost_contract_cases.py`。候选案例包括：

- Provider 租户/用途边界、原值不回显、工具输出脱敏、批量撤销和轮换幂等；
- 成本价格版本、输入/输出/缓存计量、未知价格与供应商失败、并发结算和租户隔离；
- Registry 不可变版本、Task 固定版本、canary/Promote/rollback 和 Learning 评测门禁；
- 旧客户端只看到非敏感元数据，重复请求不会产生第二份成本或发布记录。

## 复杂度决策

| 机制 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 生产 SecretStore Provider | 本地文件实现被误用于生产，或凭据跨用途读取 | 云部署计划明确正式 SecretStore 尚未完成 | 复用现有 Provider，补生产适配和主体绑定 | 缩小密钥暴露面 | 每次签发/读取一次 Provider 请求 | 中高，需 Go/Python 一致性和身份适配 | OpenBao/KMS、CA、轮换和监控 | Provider 不可用阻塞工具调用 | R-04 / D-09 后 | KEEP |
| 写入前脱敏 | Secret 进入事件、trace 或 ObjectStore 后无法回收 | 现有 Collector 脱敏不能覆盖业务持久化 | 在事件/trace/ObjectStore 边界共用最小脱敏函数 | 降低泄露影响 | 每次写入一次字段/内容扫描 | 中 | 规则版本和误报处理 | 误删用户需要的非敏感输出 | R-04 首版 | KEEP |
| 批量撤销/轮换 | 泄露凭据仍可被新调用使用，逐条操作产生部分成功 | 生产密钥需要轮换与应急撤销 | 以引用和租户范围执行幂等批处理 | 快速收回权限 | 按批次增加 SecretStore 调用 | 中 | 审计、失败重试和窗口安排 | 部分批次完成 | R-04 第二阶段 | KEEP |
| 唯一成本事件 | Task、Worker、Gateway 各自累计导致金额漂移 | Gateway 是模型调用唯一出口，Task State 仅有摘要 | 在 Gateway 结算处写唯一事件，其他组件只引用 | 可对账和预算复用 | 每次调用一次计量写入 | 中高，涉及供应商 usage 与存储 | 对账、告警和保留策略 | 结算写入失败影响调用结果 | R-06 首版 | KEEP |
| 价格版本固定 | 价格表更新后历史调用无法重算 | Profile 已有 `price_at`，Registry 版本追加 | 成本事件记录 profile/price version 与生效日 | 历史可复算 | 事件增加少量元数据 | 中 | 价格发布和纠错流程 | 供应商账单与本地价格不同 | R-06 首版 | KEEP |
| Registry 追加版本 + rollback | Promote 覆盖旧内容，运行中 Task 行为漂移 | 设计书规定版本不可变和 Task pin | 新版本写入，默认指针只指向已发布版本 | 可审计、可回退 | 一次版本查询/指针更新 | 中 | 版本保留和回滚演练 | 默认指针更新失败 | R-06 首版 | KEEP |
| Learning 评测发布门禁 | 低质量候选直接影响默认配置 | Learning 已有 Publish/Promote 和评测报告 | 缺证据或未过阈值时禁止写方法 | 降低回归风险 | 发布前增加一次评测检查 | 中 | 评测集、阈值和人工处置 | 证据过期导致发布阻塞 | R-06 首版 | KEEP |
| 第二份成本状态机/后台扫描 | 多个状态源互相漂移，或无关请求触发全量对账 | 当前没有证据需要并行账本或轮询 | 复用 Gateway 事件、Registry 版本和现有生命周期 | 保持一致且低运维 | 无额外请求和存储 | 低 | 不增加 worker/表 | 单次事件故障需明确处置 | R-04/R-06 | REJECT |

## S-01/S-02 进入实现前门槛

- 确定生产 SecretStore 的具体适配器、身份来源、CA/网络边界和租户范围；未确定前只保留候选 Provider 接口与 fixture。
- 确定成本事件的权威存储、价格版本字段、usage 延迟和未知价格策略；不在 Task State 或 Worker 内存中复制计量。
- 确定 Registry/学习发布 RPC 的错误映射、幂等键、shadow 与 default 的状态转换；任何 proto/schema 变化单独提交并运行 `buf`/生成代码/兼容性检查。
- 真实 OpenBao/Vault、供应商账单、集群脱敏、跨租户回放和故障恢复需在部署 gate 中记录；本候选基线不宣称这些外部资源已通过。
