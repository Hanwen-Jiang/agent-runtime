# Model Gateway：选档、重试与切换、计量、BYOK 与 judge 换家

> 学习笔记，2026-10-24。对应代码：Go 的网关 `services/go-model-gateway/`（`internal/router` 选档、`internal/gateway` 调用与重试、`internal/profiles`、
> `cmd/model-gateway`）、OpenAI 兼容适配层 `libs/go-core/adapters/openaicompat/`；Python 客户端 `services/py-worker/src/agentruntime_worker/executor/grpc_gateway.py`；
> 开发用 Profile `deploy/dev/model-profiles.json`。设计依据：设计书 3.3、3.3.1、4.8，附录 C.3、H；细化 `docs/plan/model-gateway-direct.md`；
> 评审 `docs/plan/review-R-02-model-gateway.md` 与另一家复审 `docs/plan/reviews/R-02-gpt-6-sol.md`。

## 1. 为什么要一个网关

所有模型调用都从一个出口走：调用方只说“我要什么档、做什么角色”，网关决定用哪个模型、怎么重试、记多少钱。好处是：

- 选模型的策略集中在一处，可以按租户约束、成本、成功率调整，不用改调用方；
- 计量、限流、密钥都在一处，调用方拿不到供应商的密钥；
- 换供应商、加模型只改 Profile。

## 2. Tier 与 Model Profile

- **Tier** 写成 `<class>@<档名>`：`small@low`、`mid@low`、`strong@medium`、`strong@high`、`embed@default`。角色有默认 Tier（附录 H.3）：分诊 small@low，Planner、Reviewer、tool_loop strong@medium。
- **Model Profile**（附录 H.1 的子集）：id、vendor、model、class、tiers（含 effort 与 thinking_budget）、capabilities、cost（输入、输出、缓存读的单价）、constraints。
- 开发用的 9 个 Profile 都经一个 OpenAI 兼容的聚合端点（a6api）；vendor 按模型所属的家登记，judge 换家靠它。

## 3. 一次调用

```
校验租户：缺租户 denied（missing_tenant），不选档、不调用
→ 选档（router.Select）：显式 Tier 或角色默认 → 租户白名单 → 能力 → avoid_vendor（有别家时排除这一家）→ pinned 排前 → 按单价升序，同价按 id
→ 按请求的租户 Bind 供应商客户端（BYOK 优先，NotFound 才用平台密钥）
→ 调用：每次尝试有自己的超时（默认 90 秒，受调用方剩余的 deadline 限制）
     transient：带抖动的指数退避重试，不短于上游给的 Retry-After；同一个 Profile 试 3 次还不行，切换同档的下一个
     model_not_found：这个 Profile 坏了，直接切下一个
     其他非 transient：立即返回
→ 计价：按未命中缓存的输入、命中缓存的输入、输出分别乘单价，精确到微美元
→ 计量：每次调用（含已计费但判为失败的）一行 JSON，带 status、pin、avoid_vendor 是否满足
```

**流式**：只在第一个分片之前重试与切换。**结构化输出**：发 `response_format: json_schema`；只有每个 object 都写了 `additionalProperties: false`、required 列全的 schema 才发 `strict: true`
（T-13 发现 OpenAI 严格模式拒绝不满足的 schema）；Claude 不执行 response_format，最后一条 user 消息里另加“只输出符合这个 schema 的 JSON”的指示。
非流式请求只接受 `application/json`：有的聚合网关看到 Accept 里有 `text/event-stream`，会把拒绝包成 200 的 SSE 流返回。

## 4. 超时要对齐

上游一次超时就用完调用方全部时间的话，网关的重试和切换根本来不及。对齐方式：网关每次尝试的超时从调用方剩余的 deadline 里分；
Python 客户端默认 deadline 300 秒，在 activity 里不超过 activity 剩下的时间减 2 秒。T-13 又发现一次完整规划（约 1.2 万输入 token、带思考）经聚合网关超过 90 秒，
而 Planner 的 activity 只有 2 分钟，悄悄回退到保守规划——plan_round、review_round、tool_loop_step 放到 5 分钟。

## 5. judge 换家（附录 H.3）

Reviewer 避开生成者那一家：Node Executor 把实际服务的 vendor 写进结果的 `meta.vendor`，tool_loop 每一步带回 vendor，Reviewer 调用时 `avoid_vendor` 填生成者最多的那一家。
网关在有别家时排除它；只剩这一家时仍可用，但在计量与日志里记 `avoid_vendor_unmet`。

## 6. 租户隔离与密钥

- 每次 RPC 按请求的租户 Bind，计量记在该租户名下；读 BYOK 遇到 transient 不退回平台 Key。
- 密钥只在 Authorization 头里；错误、日志、计量、打印形式都不出现（上游在错误正文里回显密钥也会被去掉）。
- 风险：网关信任调用方给的租户——服务间认证要靠部署层的 mTLS。

## 7. 第一期没做的（登记）

按“每次成功的期望成本”选档（H.2，现在按单价）；多供应商切换；按供应商的并发池与限流；执行 `max_cost_usd_micros` 与预算上限；数据驻留、SLA、BYOK 作为选档约束；
按 Task 固定主模型与 Profile 版本；升降级；计量进 Langfuse；history 的滚动缓存断点。

## 8. 面试速查

**Q：选模型为什么不能只看单价？**
A：真正要最小化的是“每次成功的期望成本”=（单次尝试成本 + 验证成本）÷ 成功率。便宜但常失败的模型总成本更高；输入远多于输出的调用（Planner、Reviewer）
只看输入加输出的单价也会排错。第一期按单价，是已知的简化。

**Q：重试和切换的边界怎么定？**
A：只有 transient（超时、限流、5xx）重试，带抖动的指数退避，尊重 Retry-After；同一个 Profile 有次数上限，用完换同档的下一个；只针对某个 Profile 的错误（模型下架）直接换；
参数错、权限错立即返回。还要防止重试放大：activity 自己也会重试，所以整个链路的总次数要心里有数。

**Q：为什么 Task 中途不该换主模型？**
A：换模型会让提示缓存失效、行为不一致，评测也难归因。所以同档切换只用于故障，理想是按 Task 固定主模型（第一期还没做）。

**Q：计量为什么连失败的调用也要记？**
A：上游已经计费的失败（结构化输出不合格、工具参数不是 JSON）也是钱；只记成功会让租户的成本少算，以后的预算上限也会建在错误的数上。

**Q：结构化输出要注意什么？**
A：不同供应商支持程度不同：OpenAI 的严格模式对 schema 有硬要求，不满足会拒绝整个请求；Claude 不执行 response_format，要在提示里要求只输出 JSON。
所以网关按 schema 决定发不发 strict，结果一律由调用方再按 schema 校验，不合格重问。

**Q：怎么知道提示缓存有没有生效？**
A：看计量里的 cache_read_tokens 占输入的比例。T-13 的端到端日志按调用种类（Planner、Reviewer、tool_loop 的每一步）分别算了这个比例。
