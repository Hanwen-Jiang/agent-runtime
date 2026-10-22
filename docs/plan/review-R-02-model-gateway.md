# R-02 评审：Model Gateway 选档与升降级（第一期，单供应商直连）

> 评审对象：`daf45aa`（T-05、T-06）里的 `services/go-model-gateway`、`libs/go-core/adapters/openaicompat`，以及 Worker 侧的
> `executor/grpc_gateway.py`、`executor/dev.py` 的接线。
> 依据：实施计划第 5 节 R-02 的评审重点；设计书 3.3、3.3.1、4.8、附录 C.3、H.1–H.6；测试结果。按 6.2 的模板，不看实现过程。
> 评审日期 2026-10-22。
>
> **评审不独立**：这份实现与评审出自同一个 Claude 会话，不满足实施计划 1.3 第 2 条“写审分离”。结论可以用来排出修复项，
> 但 R-02 要算完成，还需要另一家的强模型或人按同一模板再审一遍。
>
> **范围**：`docs/plan/model-gateway-direct.md` 把这一期定为“第一期”：按单价选档、单供应商、不做限流与升降级，完整版
> （期望成本、多供应商、Valkey 限流、升降级）留给实施计划第 7 节的“Model Gateway 完整版”。下面照实写“未处理”，
> 再在处理建议里区分“本期修”与“登记到完整版”。

## 测试结果（评审时）

- Go：`libs/go-core/adapters/openaicompat`（含 `modelprovidertest` 一致性测试与越权用例）、`services/go-model-gateway` 全部通过；
  适配层的真实端点一致性测试在供应商额度用完之前通过。本机缺 Postgres，相关 Go 测试按 `AR_ALLOW_MISSING_TOOLS=1` 跳过。
- Python：py-worker 604 项通过（含 GrpcModelGateway 与 Worker 接线）；跨语言端到端 23 项通过（Python 客户端 → 现场编译的网关 →
  模拟上游，含一个 model 节点经网关交付的 Task）；端到端（不含故障注入）55 项通过。

## 1. 评审重点逐条

### 1.1 先按租户约束过滤（3.3.1 核心逻辑 1，附录 H.6：模型白名单、数据驻留、预算上限、BYOK、SLA 档位）

| 约束 | 结论 | 位置与说明 |
| --- | --- | --- |
| 模型白名单 | **已处理** | `router.go:181` `Select`：先按白名单过滤（`router.go:207`），再按能力、avoid_vendor、单价；pinned 绕不过白名单（`TestPinnedProfileComesFirst`）。白名单读取失败返回原错误、不当作“不限制”（`TestAllowlistErrorIsReturned`）。开发实现读策略文件（`router.go:121`），文件不存在时不限制。 |
| 数据驻留 | **未处理** | Model Profile 没有区域字段，选档不看驻留。 |
| 预算上限 | **未处理** | `CallContext.max_cost_usd_micros` 在 proto 里有、Worker 也能传，网关一处都没读（`GetMaxCostUsdMicros` 无引用）：超预算的调用照样发出。 |
| BYOK | **不确定** | 适配层按租户先读 BYOK 密钥（`provider.go:218` `apiKey`），但 BYOK 没有作为选档时的约束：租户的 Key 用不了某个模型时，要到调用时才以 denied 或 not_found 失败，而且非 transient 不切换（见 1.3）。 |
| SLA 档位 | **未处理** | 没有对应字段。 |

### 1.2 期望成本公式实现正确（3.3.1 核心逻辑 2，附录 H.2）

**未处理。** H.2 要求在剩余 Tier 中选“每次成功的期望成本 ≈（单次尝试成本 + 单次验证成本）÷ 成功率”最低的；实现按单价
（输入单价 + 输出单价）升序选（`router.go:227`，`profiles.go:84` `UnitPrice`），没有成功率、验证成本与 token 估计，
Profile 里也没有这些数据（附录 H.1 的 capability、internal 不在第一期的子集里）。这是第一期的既定简化，但要知道它的后果：
单价忽略了输入输出比例与缓存读价格，输入远多于输出的调用（Planner、Reviewer 都是）会被排错。

单次调用的计价本身**已处理**：`profiles.go:72` `USDMicros` 按未命中缓存的输入、命中缓存的输入、输出分别计价，思考 token 算在输出里，
四舍五入到微美元（`TestCostUSDMicrosIsExact`）；缓存写入（Anthropic 按 1.25 倍计价）没有单价字段，适配层也拿不到这个数，会少算。

### 1.3 同档切换与限流（3.3.1 核心逻辑 3）

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| transient 带抖动重试 | **已处理** | `gateway.go:208` `run`、`gateway.go:242` `wait`：退避翻倍、抖动到 [d/2, d]，不短于 RetryAfter，不超过 MaxDelay（`TestRetryAfterIsHonoredUpToTheCap`）。 |
| 同档切换 | **已处理**（有缺口） | 同一个 Profile 用尽重试后切换同档的下一个（`TestSwitchesToTheNextProfileOfTheSameTier`），全部失败为 `models_exhausted`。缺口：非 transient 一律立即返回，包括只针对某个 Profile 的错误（模型在上游下架的 `model_not_found`、BYOK Key 没有这个模型的权限）；一条写错或下架的 Profile 会让整档调用失败，而同档的别的 Profile 本来可用。 |
| 切到“其他供应商” | **未处理（第一期）** | 设计说超过上限就切换同档的其他供应商；单供应商直连下同档的 Profile 都走同一个端点，端点本身不可用时切换无效。 |
| 并发池与限流 | **未处理** | 没有按供应商的并发池，也没有限流计数（设计书 3.3 说放在 Valkey）。 |
| 流式 | **已处理** | 只在第一个分片之前重试与切换（`gateway.go:435`）。 |

### 1.4 Task 中途不换主模型（3.3.1 关键规则，附录 H.4）

**未处理（机制在，没人用）。** 网关支持 `pinned_model_profile`（`router.go:229`，可选时排到最前，被白名单或 avoid_vendor
排除时忽略），但 Worker 侧没有任何地方设置它（py-worker 里只有测试支撑填了这个字段），也没有把首次调用实际用到的 Profile
记进 Task 状态。后果：
- 同一个 Task 里，某次调用因 transient 切到同档的另一个 Profile 之后，下一次调用又回到最便宜的那个，提示缓存来回失效；
- Profile 文件改了单价，运行中的 Task 会换模型；
- 3.3.1 的“只在 Task 固定的 Model Profile 版本里选档”也没有：Profile 只在启动时加载一次，没有版本，Task 也不记版本。
- `PinApplied` 算出来了但没有带回给调用方，也没写进计量或日志（`router.go:99` 只在测试里读）。调用方要自己比较 `ModelChoice`。

### 1.5 judge 用不同家的模型（3.3.1 关键规则，附录 H.3）

**不确定，实际效果是未处理。**
- 网关侧已处理：`avoid_vendor` 在有别家时排除这一家，只剩这一家时仍可用（设计说“优先”），并且优先于 pinned（`TestAvoidVendor`）。
- 但只剩同一家时的 `AvoidVendorUnmet` 没有带回调用方，也不进计量与日志：同源评判会悄悄发生。
- 调用方没有传：Reviewer 以 `role = judge`、`tier = strong@medium` 调用（`reviewer.py:151`），不设 `avoid_vendor`；Planner 也是
  `strong@medium`。按开发用 Profile，两者都落到 claude-sonnet-5-5：Reviewer 与生成者同一家，正是 H.3 要避免的。
  只有评测的 rubric judge 填了 `avoid_vendor`（`rubric.py:72`）。
- 设计书 C.3 的 Task Plan 写的是 `tiers: {reviewer: other_vendor@medium}`；选档器把 `other_vendor` 当成一个 class，
  这样的 Tier 一定是 `no_eligible_model`。

## 2. 其他风险

### 2.1 确定性

- 选档是确定的：候选按单价、再按 id 稳定排序（`TestTiesBrokenByIDAndSelectionIsDeterministic`），不依赖 map 的遍历顺序。
- 抖动用 `math/rand/v2`：网关不是工作流代码，不受 Temporal 重放约束。
- 调用方的确定性不受影响：Python 侧所有模型调用都在 activity 里。
- 没有发现确定性风险。

### 2.2 并发与重试

- **重试放大**：网关对每个 Profile 试 3 次、再切同档的每个 Profile；model 节点遇到 transient 还由 activity 整体重试
  （`stage_runner.py:919` 非写节点最多 3 次，`task.py:551` 按节点的 `retry.max`）。上游限流时，一次逻辑调用最多变成
  “activity 次数 × Profile 数 × 3”次上游请求，而且没有限流计数兜底（1.3）。Planner、Reviewer 不放大：它们吞掉错误、回退到
  保守规划或规则判定，代价是质量悄悄下降（真模型评测里就遇到了，见 TMP 计划 T-03）。
- **超时没对齐**：适配层每次请求的超时 120 秒（`provider.go:28` `DefaultTimeout`），Python 客户端每次调用的 deadline 也是 120 秒
  （`grpc_gateway.py:45`）。上游一次超时就用完了客户端的全部时间，网关的重试与切换在超时场景下实际不会发生；客户端判 transient 后
  由 activity 重试，同一个请求可能在上游被处理了两次（多付一次钱）。
- 计量写入按行加锁，并发下不串行（`TestMeterLinesStayIntactUnderConcurrency`）；选档器与 Profile 只读；适配层的模型列表缓存有锁。
  没有发现数据竞争。

### 2.3 租户隔离

- **已处理**：缺租户直接拒绝、不选档不调用（`gateway.go:181`）；每次 RPC 按请求的租户 Bind（`TestProviderIsBoundToTheRequestTenant`），
  不用平台作用域；计量记在请求的租户名下；读 BYOK 遇到 transient 不退回平台 Key（`TestPlatformKeyIsNotFallbackForSecretStoreFailure`）；
  密钥只在 Authorization 头里，错误、日志、计量、打印形式都不出现（适配层 `leak_test.go`、网关 `TestSecretsInUpstreamErrorsDoNotLeak`）。
- **风险：网关不认证调用方**。租户取自请求里的 TenantContext，网关信任调用方。集群里任何能连到网关的进程都能冒用别的租户：
  用它的 BYOK Key、记它的账、按它的白名单选档。计划把服务间认证留给部署层的 mTLS，部署时必须补上，否则 CLAUDE.md
  “租户由服务端根据认证身份确定”在这一跳上不成立。
- **风险：上游的错误说明会原样往下传**（截到 200 字、去掉密钥，`provider.go:346` `classify`）。有的上游会在说明里回显请求的参数值，
  可能带出租户的消息片段，进到 Worker 的错误、日志与 trace。错误模型要求错误信息里不出现租户的原始数据。

### 2.4 计量的完整性（3.3.1 核心逻辑 4）

只有成功的 RPC 写计量。上游已经计费、但网关判为失败的调用不记：结构化输出不是合法 JSON（`bad_structured_output`）、
Score 个数不对、Classify 标签不对、流式中途断开、重试中途失败的尝试。真实端点上 Claude 不执行 `response_format`，
结构化输出失败并不少见。按租户的成本因此少算，以后的预算上限（1.1）也会建立在少算的数上。

## 3. 与设计书不一致的地方

1. 选档按单价，不按每次成功的期望成本（H.2）。第一期的既定简化。
2. 没有限流与并发池，没有 Valkey（3.3）；Profile 从文件读、不从 Registry 读，不按 Task 固定版本（3.3、3.3.1）。
3. 没有升降级（3.3.1 核心逻辑 5、H.4）：网关不按连续失败或分歧升级；Profile 的 `effort` 读进来但没用（`profiles.go:55`），
   推理强度只由思考预算按区间推出（适配层 `EffortForBudget`）；`CallContext.reasoning` 也没用。
4. 缺租户时网关报 **invalid**（`missing_tenant`），而 S-02 的统一约定（`libs/go-core/tenancy`、Python 侧同名模块）是 **denied**。
   网关的验收测试按 invalid 写，两边语义不一致。
5. Reviewer 的 Tier 写法：设计书 C.3 是 `other_vendor@medium`，实现没有这个概念（1.5）。
6. 模型调用的语义 span 没有送 Langfuse（3.3）；计量只写标准输出。

## 4. 处理建议

TMP 计划第 0 节第 4 条：未处理的修掉或登记。建议分两类：

**本期修**（小，而且不修会让上面的结论失真）：
1. Reviewer 避开生成者那一家：Reviewer 调用时填 `avoid_vendor`（Planner 那一轮的 `ModelChoice.vendor` 已经记在 Planner 的计量里，
   可以经 Task State 传过去）；或者先按 C.3 支持 `other_vendor@<tier>`：网关把它解释为“strong 档里与 `avoid_vendor` 不同家的”。
   要动 Task Loop 的数据流，先请人定方案。
2. `PinApplied`、`AvoidVendorUnmet` 写进计量记录与日志，同源评判、pin 被忽略都看得见。
3. 已计费但判为失败的调用也写计量（带上失败的 code），按租户的成本不再少算。
4. 超时对齐：网关每次尝试的超时从调用方剩余的 deadline 里分，客户端 deadline 留出重试与切换的时间。
5. 只针对某个 Profile 的非 transient 错误（`model_not_found`）切换同档的下一个 Profile，不让一条坏 Profile 拖垮整档。
6. 缺租户的类别与 S-02 对齐成 denied（要改网关的验收测试，先请人确认）。

**登记到完整版**（写进 `docs/reference/risks-and-open-questions.md`）：期望成本选档、多供应商切换、按供应商限流与并发池、
`max_cost_usd_micros` 与预算上限、数据驻留与 SLA 档位、BYOK 作为选档约束、按 Task 固定 Profile 版本与主模型（要 Task State 配合，
属于核心链路，单独排期）、升降级、Langfuse span、上游错误说明的脱敏、服务间认证（部署层 mTLS）。
