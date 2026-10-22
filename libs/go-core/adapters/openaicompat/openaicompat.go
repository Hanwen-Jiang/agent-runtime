// Package openaicompat 是 ModelProvider 的 OpenAI 兼容 HTTP 适配层（设计书 4.8、附录 L.9；
// 工作项“Model Gateway 单供应商直连”，docs/plan/model-gateway-direct.md）。语义见 providers/modelprovider。
//
// 只做协议转换：选档、重试、切换 Model Profile、计价都在 Model Gateway 里。本层不重试，每次调用只向上游发一个请求。
// 只用标准库 net/http，不引入 SDK。
//
// 端点（BaseURL 形如 https://host/v1，末尾的 / 可有可无）：
//   - Generate、StreamGenerate、Score：POST {BaseURL}/chat/completions
//   - Embed：POST {BaseURL}/embeddings
//
// 凭据：经 SecretStore 读取名为 KeyName 的密钥，只放进请求的 Authorization: Bearer 头；URL（含查询串）、请求体与其他请求头
// 里都不出现，也不另发 api-key、x-api-key 之类的头。
// 错误文本、错误链、客户端与 Provider 的打印形式里都不出现密钥，即使上游把它回显在错误正文里（适配层先去掉密钥再引用上游的
// error.message）。客户端里的密钥用 secret.Value 保存。
//   - Bind(tenant)：先读租户作用域的同名密钥（BYOK）；租户作用域返回 NotFound 时再读平台作用域。
//     读租户作用域遇到其他错误（例如 Transient）照原类别返回，不退回平台密钥。
//   - BindPlatform(purpose)：只读平台作用域。
//   - 两处都没有密钥：Denied，不调用上游。
//   - 租户绑定规则同 tenancy：缺少租户 Denied（missing_tenant），格式不对 Invalid（malformed_tenant），平台作用域要写明用途
//     （Invalid，missing_purpose）。Bind 时复制租户上下文。
//
// 请求映射（GenerateRequest → chat/completions）：
//   - Model → model。Messages 按角色映射为 system、user、assistant、tool；assistant 的 ToolCalls → tool_calls
//     （type 为 function，arguments 为 JSON 字符串）；tool 消息带 tool_call_id。
//   - Tools → tools（type 为 function，function.parameters 为 ParamsSchema）。
//   - CacheBreakpoint 为 true 且文本非空的消息：content 写成内容片段数组，文本片段带 cache_control {"type":"ephemeral"}
//     （Anthropic 风格的提示缓存断点，聚合网关转给支持的模型）。2026-10-21 实测 a6api 上 Claude 与 GPT 都接受这种写法。
//   - OutputSchema 非空 → response_format {"type":"json_schema","json_schema":{"name":…,"schema":<OutputSchema 原样>,"strict":true}}。
//     有的端点不执行 response_format（实测：聚合网关后面的 Claude 原样回答文本），所以另在最后加一条 user 消息，写明只输出
//     符合该 schema 的 JSON。解析时容忍 ```json 代码块。
//   - MaxOutputTokens 大于 0 → max_completion_tokens（或 max_tokens）；为 0 时不发送。
//   - ThinkingBudgetTokens → reasoning_effort = EffortForBudget(ThinkingBudgetTokens)；为空串时不发送该字段。
//   - StreamGenerate 发送 "stream":true 与 "stream_options":{"include_usage":true}；非流式请求不带 stream_options。
//
// 响应映射：
//   - message.content → Text；message.reasoning_content → Thinking（只写入 trace，不混进 Text 与 Structured）。
//   - message.tool_calls → ToolCalls（Arguments 为参数的 JSON）。
//   - finish_reason：stop → stop，length → max_tokens，tool_calls → tool_calls，content_filter → content_filter（不是错误）。
//   - OutputSchema 非空时 content 必须是合法 JSON，放进 Structured；不是合法 JSON 报 Invalid。
//     完整的 schema 校验在 Python 侧（Go 侧没有 JSON Schema 库）。
//   - usage：prompt_tokens → InputTokens（含缓存命中）；prompt_tokens_details.cached_tokens → CacheReadTokens；
//     completion_tokens → OutputTokens（含思考）；completion_tokens_details.reasoning_tokens → ReasoningTokens。
//     CostUsdMicros 为 0，由 Model Gateway 按 Model Profile 计价。
//   - 流式：reasoning_content 的分片 → ThinkingDelta；content 的分片 → TextDelta；工具调用的参数分片拼完整后才以
//     Chunk.ToolCall 交出（参数是完整的 JSON）。最后一个分片 Final 带完整结果与 Usage（取自 include_usage 的末尾分片），
//     之后 Recv 返回 io.EOF。SSE 注释行跳过。上游没有发出 [DONE] 就断开：Transient。
//
// Score：端点没有打分接口，转成一次结构化输出的生成（协议转换）。response_format 的 schema 为
//
//	{"type":"object","properties":{"scores":{"type":"array","items":{"type":"number"},"minItems":N,"maxItems":N}},
//	 "required":["scores"],"additionalProperties":false}
//
// N 为候选个数；提示里给出 Query、Rubric，并按顺序列出全部候选。上游回复形如 {"scores":[…]}，第 i 个分数对应第 i 个候选。
// 分数个数不等于 N、或回复不是合法 JSON：报错，不返回部分结果。Usage 照生成的映射。
// 真实端点背后各家模型在严格模式下是否接受 minItems、maxItems 未经验证：live 测试 TestLiveScoreOnJudgeProfiles 对
// Model Profile 文件里 strong@medium（judge 的默认 Tier）的每个 Profile 各跑一次 Score。有一家不接受（400），就去掉这两个
// 关键字，只靠本层自己检查个数（TestScoreRejectsWrongNumberOfScores 已覆盖）；届时 fakeopenai 的 Sample 不再能从 schema
// 得知数组长度，要相应调整模拟服务。
//
// Embed：input 为 Texts 的数组；按响应里每项的 index 对回输入顺序；Usage 的 InputTokens 取 prompt_tokens。
//
// 错误分类（HTTP 状态与正文的 error.code；可否重试由状态码决定）：
//   - 状态码 404，或 4xx 且 error.code 为 model_not_found：NotFound。
//   - 400：Invalid（其余 4xx 也按 Invalid）。401、403：Denied。
//   - 按上面归为 Invalid 时，查一次 GET /models（按密钥的哈希缓存 5 分钟）：请求的模型确实不在列表里，改为 NotFound
//     （code model_not_found）；列表取不到时保持 Invalid。2026-10-21 实测 a6api 对未知模型只回笼统的 400（invalid_request）。
//   - 429：Transient，RetryAfter 取自 Retry-After 头（秒数）。500、502、503（其余 5xx 也一样）：Transient。
//   - 5xx 且 error.code 为 model_not_found：仍是 Transient。one-api、new-api 一类聚合网关用 503 + model_not_found 表示
//     “该模型此刻没有可用渠道”，是暂时的；按 Transient 处理，Model Gateway 才会重试并切换同档的下一个 Model Profile
//     （附录 H.1 reliability.fallbacks）。代价：这类网关对未知模型也这样返回，Model Profile 里写错的模型名表现为 transient
//     而不是 not_found。这一条待人确认；真实端点的实际返回由 live 测试记录。
//   - 超时（Options.Timeout 或调用方 ctx 的截止时间）、网络错误、响应不是合法 JSON：Transient。
//
// 所有错误都是 *providererr.Error。
package openaicompat
