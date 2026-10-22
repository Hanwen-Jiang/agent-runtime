# Model Gateway 单供应商直连（中档工作项细化）

> 实施计划第 7 节阶段 1 的中档工作项“Model Gateway 单供应商直连”（中 · 中）。第 7 节说“细化另行编写”，这里是细化。
> 依据：设计书 3.3.1、4.1、4.8、4.12、附录 H.1–H.4、L.9、L.17；S-02 的 ModelProvider 接口与一致性测试。
> 完整版（多供应商、按期望成本选档、Valkey 限流、升降级）是第 7 节阶段 4 的“Model Gateway 完整版”，由 R-02 评审，不在这里。

## 目标

让 Worker 的模型调用（Node Executor、Planner、Reviewer、评测 harness）经真正的 Model Gateway 走到一个真实的供应商。
接好后，S-09 与 S-10 可以在真模型上跑评测门槛，阶段 3 的“每次成功成本”也有了计量来源。

供应商是一个 OpenAI 兼容的 HTTP 端点（聚合多家模型）。“单供应商”指只有一个供应商适配层与一个端点；
端点背后的模型分属不同的家（vendor），Model Profile 照实登记，judge 可以按 `avoid_vendor` 选另一家。

## 交付

1. **供应商适配层** `libs/go-core/adapters/openaicompat`：实现 `modelprovider.Provider`（设计书 4.8：只做协议转换）。
   - `/chat/completions`（含 SSE 流式、工具调用、`response_format: json_schema`）与 `/embeddings`；只用标准库 `net/http`，不引入 SDK。
   - Score：端点没有打分接口，转成一次结构化输出的生成（每个候选一个分数），属于协议转换。
   - 凭据经 SecretStore 读取：`Bind(tenant)` 先读租户作用域的同名密钥（BYOK），没有再读平台作用域；`BindPlatform` 只读平台作用域。
   - 思考预算按区间映射到 `reasoning_effort`（0 不发送）；`reasoning_content` 映射为 Thinking（只写入 trace）。
2. **Model Gateway 服务** `services/go-model-gateway`（connect-go，实现 `ModelGatewayService` 的五个方法）：
   - Model Profile：开发环境从 JSON 文件读取（附录 H.1 的子集：id、vendor、供应商模型名、模型档 class、Tier 列表、能力、单价、约束、取价日期）；
     Tier 写作 `<class>@<tier>`，例如 `strong@medium`。仓库里放一份开发用的 `deploy/dev/model-profiles.json`，单价标注取价日期，待按供应商实价更新。
   - 选档（第一期）：先过租户约束（模型白名单）与能力（结构化输出、工具、嵌入），再按 `pinned_model_profile`、`avoid_vendor` 筛选，
     取单价最低者，按 id 打破平局。`tier` 为空时按 role 的默认 Tier（附录 H.3）。
   - 调用：transient 失败带抖动重试（遵守 RetryAfter，有上限）；重试用尽后切换同档位的下一个 Model Profile；全部失败返回 transient。
     非 transient 不重试。
   - 计量：每个响应带 Usage；`cost_usd_micros` 按 Model Profile 单价计算；每次调用写一条计量记录（租户、Task、Node、role、模型、Tier、token、成本），
     不含消息内容与密钥。
   - Classify：转成结构化输出的生成，标签只能取请求给的值。
   - 错误：providererr 的类别映射到 Connect 错误码，并在 trailer `ar-error-bin` 里附上序列化的 `common.v1.Error`，Python 侧据此还原错误类别。
   - 命令：`model-gateway serve`（`/healthz` 就绪检查）；`model-gateway dev-secret <名称>` 从标准输入读密钥写入开发 SecretStore 文件，
     密钥不经过命令行参数。
   - 配置（环境变量）：`AR_MODEL_GATEWAY_ADDR`、`AR_MODEL_PROFILES_FILE`、`AR_MODEL_PROVIDER`（`openai_compat` 或 `mock`）、
     `AR_MODEL_PROVIDER_BASE_URL`、`AR_MODEL_PROVIDER_KEY_NAME`、`AR_SECRETS_FILE`、`AR_DEV_TENANT_POLICIES_FILE`（可选的模型白名单）。
3. **Python 客户端** `services/py-worker/src/agentruntime_worker/executor/grpc_gateway.py`：`GrpcModelGateway` 实现 `ModelGateway` Protocol（grpcio）；
   按 `ar-error-bin` 还原 ProviderError，没有时按 gRPC 状态码映射；`from_env()` 可直接作为 `AR_EVAL_GATEWAY_FACTORY`。
   Worker 设 `AR_MODEL_GATEWAY_ADDRESS` 时，Node Executor、Planner、Reviewer 都改用它；不设时仍是桩模型（端到端测试的默认不变）。

## 难点与防错

- **密钥不外泄**：密钥只在适配层发请求时出现在 `Authorization` 头里；错误信息、日志、计量记录、trace 都不出现密钥，即使上游把它回显在错误正文里。
- **租户上下文**：缺少租户的请求直接拒绝，不调用供应商；供应商客户端按请求的租户绑定（`Bind`），计量记在该租户名下。
  Model Gateway 是内部服务，租户来自 Worker 转发的 TenantContext（源头是 task-service 的认证身份）；服务间认证留给部署层（mTLS），不在本项。
- **错误分类一致**：上游 429、5xx、超时为 transient（429 带 RetryAfter）；400 为 invalid；401、403 为 denied；模型不存在为 not_found。
  Go 与 Python 两侧对同一错误给出同一类别；只有 transient 可重试。
- **结构化输出**：靠上游的 `json_schema` 严格模式；适配层检查结果是合法 JSON，完整的 schema 校验在 Python 侧（Node Executor、Planner、Reviewer 已经在做）。
  Go 侧没有 JSON Schema 库，引入需要人确认（设计书 4.15 之外的依赖）。
- **流式**：请求 `stream_options.include_usage`；各分片文本依次拼接等于最后一个分片的文本；最后一个分片带 Usage 与成本。
- **确定性的选档**：同样的输入与 Profile 选出同一个模型（单价相同按 id），便于测试与复现。
- **rationale 只进 trace**：思考内容放进 `GenerateResponse.rationale`，不混进 `text` 或 `result`。

## 验收

- 适配层：在本地模拟的 OpenAI 兼容服务上跑通 `modelprovidertest` 一致性测试（含越权用例，满足 S-12a 的隔离覆盖检查）；
  请求与响应映射、错误分类、密钥不外泄各有测试。
- 服务：选档（默认 Tier、显式 Tier、pinned、avoid_vendor、白名单、无可用模型）、缺租户拒绝、成本计算精确到 micros、transient 重试与同档切换、
  非 transient 不重试、流式最后分片带计量、Classify 只出给定标签、计量记录不含内容与密钥，各有测试。
- 跨语言：Python 的 `GrpcModelGateway` 经真实编译的 `model-gateway`（上游为本地模拟服务）完成生成与嵌入，租户与计量往返一致，
  错误类别在 Python 侧还原正确；一个 Task 的 model 节点经这条链路交付。
- 真实端点（设了 `AR_LIVE_MODEL_PROVIDER=1` 才跑）：适配层一致性测试在真实端点上通过；Model Gateway 按 small、mid、strong 各完成一次生成，
  并完成一次结构化输出与一次嵌入。
- 现有测试（py-worker、端到端、契约、Go）全部仍然通过。

## 模型与评审

中档 · 中（计划第 7 节）。第 7 节没有为这一项指定强档评审；选档与升降级的完整版由 R-02 评审。

## 依赖

S-02 的 ModelProvider 接口、一致性测试与 SecretStore 开发实现；S-12a 的隔离覆盖检查。

## 实施记录（2026-10-22，TMP 计划 T-05、T-06）

验收测试是 2026-10-21 写的（实现当时是桩），经人确认后接手实现。与上文约定不同、或约定没写到的地方：

- **真实端点的实测**（a6api，聚合多家模型）：
  - `response_format` 的 `json_schema` 严格模式：GPT 照办，Claude 不理会（原样回答文本）。适配层因此另在最后加一条 user 消息，
    写明只输出符合 schema 的 JSON，并容忍 ```json 代码块。
  - 未知模型回笼统的 400（`error.code` 为 `invalid_request`），不是 404。适配层在归为 Invalid 时查一次 `GET /models`
    （按密钥哈希缓存 5 分钟），模型确实不在列表里就改报 NotFound（`model_not_found`）；`TestLiveErrorsAreClassifiedWithoutLeakingKey`
    相应加了 400 这一支。
  - `max_tokens` 与 `max_completion_tokens` 都接受，用后者；`cache_control` 内容片段 Claude 与 GPT 都接受，断点原样转成
    `cache_control: {"type": "ephemeral"}`。
  - 额度用完时回 403 `insufficient_user_quota`，按通则为 Denied（不重试、不切换）。
- **开发用 Model Profile**：claude-sonnet-5-5 的单价改为一方标价 $2/$10（缓存读 $0.2）；新增 claude-opus-5-5（strong）与
  claude-haiku-5-5（small），一方标价。选档取单价最低：small 首选 gemini-3.5-flash-lite，mid 首选 deepseek-v4-flash，
  strong 首选 claude-sonnet-5-5。与临时 Gateway（strong 用 Opus 5.5）的评测结果不能直接比较；要对照时用 `pinned_model_profile`。
- **Tier 名**：Context Builder 的摘要与 Probe 原来用 `light@low`，选档器只认 small、mid、strong、embed，改为 `small@low`
  （Planner 的分诊本来就是 `small@low`）。临时 Gateway 把 small 与 light 当作同一档。
- **Worker 接线**：`executor.dev.model_gateway(config)` 是唯一的入口，Node Executor、Planner、Reviewer 与 tool_loop（`AR_TOOL_LOOP=model`）
  都经它；`serve` 在 `closing_model_gateways()` 里组装，同一地址共用一个 GrpcModelGateway，退出时关闭。
- **评测改用 Go Gateway**：`AR_EVAL_GATEWAY_FACTORY=agentruntime_worker.executor.grpc_gateway:from_env`，再设 `AR_MODEL_GATEWAY_ADDRESS`。
  `provider_gateway.py` 保留作离线对照，退役放到 T-17。
- **没做的**：TMP 计划 T-05 的“history 最后一条消息加滚动断点”与设计书附录 G.1（断点只在 system、profile、brief 末尾）不一致，
  没有改 Context Builder，见回复里的设计改动建议；命中率对比要真模型，供应商额度用完，待补。
