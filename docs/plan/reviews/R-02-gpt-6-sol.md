# R-02 复审（另一家的强模型：OpenAI gpt-6-sol，经 a6api）

> 2026-10-24。按实施计划 6.2 的模板，只给了工作项全文、设计书相关章节、非测试源码的 diff 与测试结果（材料包的组成见 [README.md](README.md)），
> 没有给之前同一家模型写的评审。下面是模型输出的原文，没有改动；采纳与处理见对应评审文档的“另一家复审”一节。

## 1. 逐条检查“评审重点”与“难点与防错”

### 评审重点

| 检查项 | 结论 | 依据与代码位置 |
|---|---|---|
| 先按租户约束过滤 | **未处理** | `services/go-model-gateway/internal/router/router.go`：`Router.Select` 在选候选时应用模型白名单，并在此后处理 `avoid_vendor`、价格排序和 pin。但白名单是唯一实现的租户约束；没有按数据驻留、预算上限、BYOK 策略或 SLA 档位过滤。凭据层的 BYOK 读取不等于选档前按 BYOK 策略剔除 Tier。 |
| 期望成本公式实现正确 | **未处理** | `services/go-model-gateway/internal/router/router.go`：`Router.Select` 仅按 `Cost.UnitPrice()`（输入单价＋输出单价）排序；`services/go-model-gateway/internal/profiles/profiles.go`：`Cost.UnitPrice`。未使用 H.2 的成功率，也没有单次验证成本，因此当前选择目标不是“每次成功交付的期望成本”。 |
| 同档切换与限流 | **未处理** | `services/go-model-gateway/internal/gateway/gateway.go`：`Server.run` 对候选执行 transient 重试并切换同档候选；`router.Router.Select` 提供同档候选排序。但未见并发池或限流实现，也未见共享 Valkey 限流计数；设计书 3.3.1 要求按供应商维护并发池与限流。 |
| Task 中途不换主模型 | **未处理** | `services/go-model-gateway/internal/router/router.go`：每次 RPC 独立执行 `Select`；`services/go-model-gateway/internal/gateway/gateway.go`：`Server.run` 可在同档 Profile 间切换。请求的 `pinned_model_profile` 只是单次调用的偏好，材料未展示 Task 级固定 Profile 版本或跨调用固定主模型的机制。 |
| judge 用不同家的模型 | **未处理** | `services/go-model-gateway/internal/router/router.go`：支持 `avoid_vendor`，但调用方必须传入该值；提供的 `reviewer.py` 第 147–155 行创建 `CallContext` 时只传 `role`、`tier`，没有设置 `avoid_vendor`，也没有生成者 vendor 信息可供比较。因此 Reviewer/judge 实际上没有保证选择不同家。 |

### 难点与防错

| 检查项 | 结论 | 依据与代码位置 |
|---|---|---|
| 密钥不外泄 | **已处理** | `libs/go-core/adapters/openaicompat/provider.go`：`classify`、`sanitize` 对上游错误正文去密钥并限制长度；请求只把 Key 放入 Authorization 头；`Format` 不打印密钥。`services/go-model-gateway/internal/gateway/errors.go`：非 Provider 错误不带底层错误文本。`MeterRecord` 不含消息或密钥。 |
| 租户上下文 | **已处理** | `services/go-model-gateway/internal/gateway/gateway.go`：各 RPC 先经 `checkTenant`，再由 `prepare` 选档并按该租户 `Bind`；适配层 `client.apiKey` 先读租户 BYOK，NotFound 才退回平台 Key。缺少租户不会调用供应商。租户身份的可信来源与服务间认证依实施记录由部署层负责，本材料无法验证部署配置。 |
| 错误分类一致 | **不确定** | Go 侧在 `provider.go` 的 `classify` 分类，Connect trailer 由 `gateway/errors.go` 的 `ConnectError` 编码，Python 在 `grpc_gateway.py` 的 `restore_error` 还原。所给测试结果覆盖 Go 测试与 Python 客户端测试，但没有列出所有状态码对应的跨语言断言结果；还需查看具体错误分类测试及完整跨语言错误用例结果。 |
| 结构化输出 | **未处理** | `libs/go-core/adapters/openaicompat/wire.go` 的 `structured` 只检查 JSON 合法性，没有在 Go 侧校验 schema；材料明确完整 schema 校验在 Python 侧。对不执行 `response_format` 的端点仅追加提示并容忍代码块，不能保证上游严格遵守 schema。 |
| 流式 | **不确定** | `libs/go-core/adapters/openaicompat/stream.go`：`finishStream` 汇总 Text、Usage 并发 Final；Gateway 的 `StreamGenerate` 转发分片并只在 final 写成功计量。测试结果列有 `stream_test.go`，但没有给出断流、分片拼接、最后分片 Usage/成本等具体结果；还需查看这些断言及真实端点测试覆盖。 |
| 确定性的选档 | **已处理** | `services/go-model-gateway/internal/router/router.go`：Profile 按 ID 排序，候选按单价、ID 排序，满足同输入下的确定性排序；allowlist 文件每次读取。此结论仅针对选档，不代表调用重试、供应商结果或运行期策略具备确定性。 |
| rationale 只进 trace | **已处理** | `services/go-model-gateway/internal/gateway/gateway.go`：`generateResponse` 将 Thinking 放入 `rationale`，Structured 放入 `result`，不将 Thinking 混入 Text 或 Structured。 |

## 2. 其他风险与不一致

### 确定性风险（Temporal 工作流重放）

- **是否会破坏 Temporal 工作流重放：不确定。** 改动中的 Python Gateway 客户端会发起外部 gRPC 调用，Go Gateway 重试抖动使用随机数（`gateway.New` 的 `Jitter`），但材料没有给出这些调用是否仅发生在 Temporal Activity 内、是否有 workflow 代码直接调用的调用链。需要检查 Planner、Reviewer、Node Executor 的实际执行边界及相关 Workflow 实现。
- **Activity 重试可能重复产生供应商调用：仍有风险。** Python `GrpcModelGateway._deadline` 试图让 RPC deadline 早于 Activity 截止时间，Go Gateway 也按调用 deadline 限制尝试；但这不能证明 Activity 被取消或 Worker 故障时供应商侧一定没有继续处理，也没有看到幂等键或按请求去重机制。若 Activity 重试，需确认能接受重复计费，或补充幂等/去重设计。

### 并发风险

- **限流与并发保护缺失：确定。** Gateway 仅有计量写入的互斥锁；未见供应商并发池、限流器或共享 Valkey 计数器。多副本下本地互斥也无法提供全局限流。
- **流式重试与取消行为需进一步验证：不确定。** `StreamGenerate` 在首分片前重试，首分片后不切换；实现通过超时 context 控制首分片，但材料没有展示并发取消、客户端断开后供应商流是否及时关闭的测试结果。

### 租户隔离风险

- **租户策略覆盖不足：确定。** `router.FileAllowlist` 只实现模型白名单；驻留区域、预算、SLA 等策略不会参与过滤。租户上下文虽然在服务入口校验，但服务间认证被明确留给部署层，本材料不能判断运行环境是否实际启用可信身份传递或 mTLS。
- **BYOK 未作为选档约束：确定。** 适配层会按租户取 Key，但 Router 不知道租户是否必须 BYOK，也不会据此过滤模型或供应商。

### 与设计书不一致的地方

- H.2 的“每次成功的期望成本”没有实现；当前仅按输入单价与输出单价之和排序。
- 3.3.1 的数据驻留、预算、BYOK、SLA 租户过滤未实现。
- 3.3.1 的供应商并发池、限流及 Valkey 共享计数未实现。
- H.4 要求主模型尽量固定；当前每次 RPC 可独立选档，失败时可换同档 Profile，未见 Task 级固定版本机制。
- H.3 要求 judge 优先使用不同家族模型；Reviewer 调用未传 `avoid_vendor`。
- 3.3 说明 Model Profile 与租户策略从 Registry 读取，价格、延迟与可用性用实时数据；本实现从本地 JSON 和开发白名单文件读取，且单价静态排序。
- 3.3.1 要求计量写入 trace；当前 `main.go` 将计量 JSON 行写到 stdout，未见写入 Langfuse 或其他 trace 系统。
- 实施记录指出 T-05 的“history 最后一条消息加滚动断点”未实现；同时记录其与设计书附录 G.1 的断点位置约定不一致，需由设计变更决定，而非仅视为实现遗漏。
- 评审修复增加了失败计量与偏好标记，但材料未证明所有“已计费失败”都能带回 Usage；例如上游错误响应或断流已计费但没有 Usage 的情形，无法从现有材料判断是否会漏记。

## 3. 按严重程度排序的问题清单

1. **缺少并发池与共享限流，导致多副本可能超过供应商限额并引发集中 429；建议实现按供应商的并发控制及共享 Valkey 限流。**
2. **租户过滤只覆盖模型白名单，驻留、预算、BYOK 与 SLA 约束可能被违反；建议补齐策略输入并在候选进入价格排序前统一过滤。**
3. **选档按单价而非成功率调整后的期望成本，可能持续选中“便宜但成功率低、总成本更高”的模型；建议纳入 H.2 成功率及验证成本数据。**
4. **没有 Task 级固定 Profile 版本，调用重选及同档切换可能改变主模型并丢失提示缓存；建议把固定 Profile 版本纳入 Task 上下文，并限定主模型切换边界。**
5. **Reviewer 未传 `avoid_vendor`，无法落实 judge 与生成者不同家的要求；建议记录生成者 vendor，并在 Reviewer 调用时传入对应避让约束。**
6. **Temporal Activity 取消或重试仍可能造成重复供应商调用和计费，且调用是否始终位于 Activity 内不明；建议核实调用边界，并补充幂等/去重或明确重复计费处理策略。**
7. **结构化输出只验证 JSON 合法性而不验证 schema，追加提示不能保证模型满足结构约束；建议在 Gateway 侧增加 schema 校验，或明确由调用方校验失败并计量的完整契约。**
8. **计量当前写 stdout，未满足设计书写入 trace 的要求；建议接入持久化计量/trace 出口，并验证失败计量在进程重启和多副本下不丢失。**
