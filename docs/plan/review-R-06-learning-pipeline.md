# R-06 评审：Learning Pipeline 发布门槛

> 评审对象：`9edb925` 里 T-16 的部分：`services/py-learning/src/agentruntime_learning/pipeline`，Worker 收尾写回给它的 trace
> （`reviewer/wrap_up.py` 的 `invoke_workflow`、`runtime.round`），以及它依赖的 Registry 发布路径（task-service `internal/registry` 的 Publish、Promote）。
> 依据：实施计划第 5 节 R-06 的评审重点；设计书 3.5、3.5.1、附录 J.1–J.5、H.2；细化 `docs/plan/learning-pipeline.md`；测试结果。
> 按 6.2 的模板，不看实现过程。评审日期 2026-10-24。行号按 `db72b28`。
>
> **独立性**：同 R-01：新会话、没看实现过程，评审者与实现者同一家模型。T-16 是中档工作项，强档以高推理来审合乎 1.3 第 2 条第 2 款；
> 严格的写审分离还要另一家的强模型或人。

## 测试结果（评审时）

- `services/py-learning` 下 `-k pipeline`：76 项通过（不跳过）。
- 端到端演示 `tests/e2e/test_learning_pipeline.py` 这次没有重跑；细化第 6 节记的是 2026-10-23 通过。
- 真模型的失败归因没跑过（细化第 6 节“没验证的”）。

## 1. 评审重点逐条

### 1.1 未过评测不能发布

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 门槛在发布之前 | **已处理** | `run.py:198`：门槛不通过即返回 rejected，`publish`、`promote` 只在之后出现；dry-run 也不写（`:202`）。测试用记录调用的 Registry 断言写方法零调用。 |
| 第二道 | **已处理** | Registry 的 Promote 按 schema 校验证据，`passed` 不是 true 就拒（`service.go:154`）。Publish 只建影子版本；`ListRecipes` 只列默认版本（`visibleRecipes`），影子版本不会被 Planner 看到。 |
| 样本不足 | **已处理** | 两组都至少 `min_samples`（默认 100），不足直接不通过（`evaluation.py:123`）；不因“差异不显著”放行。 |
| 选择偏差 | **已处理** | 候选组含按这条路失败的、以及中途失败的 Task（`evaluation.py:87`）。 |
| 每次成功成本的口径 | **未处理** | `Outcomes.cost_per_success` 是**成功 Task 成本的均值**（`evaluation.py:51`、`:60`），不含失败的花费。附录 H.2 的定义是“（尝试成本 + 验证成本）÷ 成功率”，S-08 的评测 harness 也按“总成本 ÷ 成功数”算（报告里的 `per_success_usd`）。失败多而贵的做法在成本门槛上看起来更便宜；两条门槛都卡在 δ = 2 个百分点、ε = 5% 附近时，这个差别足以改变结论。 |
| Task 成本本身少算 | **未处理** | trace 上的 Task 成本是各 Round `cost_usd` 之和（`wrap_up.py:87`），而 Round 的成本只算 Atomic Node 的执行（见 R-01 评审 2.2）：tool_loop 每一步、Planner、Reviewer 都不在里面。学 Recipe 最主要的收益正是省掉完整规划，这个门槛量不到；以 tool_loop 为主的任务族，成本门槛几乎恒为 0 比 0。 |
| 证据可信度 | **风险** | Registry 只看证据的 schema 与 `passed`，统计是 Pipeline 自己算、自己报的；能拿到发布令牌的进程就能带一份 `passed: true` 的证据晋升任意 Recipe。Learning 用的是平台作用域的共用发布令牌（细化第 4 节第 5 条），没有自己的角色与令牌。 |

### 1.2 后台任务单独记预算

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 一次运行一个预算 | **已处理** | `LearningBudget`（`budget.py`）；每次调用前看是否用完，用完一律 unattributed（`attribution.py:93`）；花费记平台作用域的 `learning.cost_usd`（`run.py:230`）。 |
| 单次调用不超支 | **未处理（依赖网关）** | 每次调用填 `max_cost_usd_micros = 剩余预算`（`attribution.py:80`），但 Model Gateway 不读这个字段（R-02 评审 1.1“预算上限”：`GetMaxCostUsdMicros` 无引用）。预算只在两次调用之间检查，最后一次可以超出。small@low、提示很短，超出有限。 |
| 跨运行的总额 | **未处理** | 预算只按一次运行；KEDA ScaledJob 反复拉起时没有按天或按租户的总额。设计书 J.5“按价值抽样”也没有：每个失败的 Task 都归因一次，只受这一次运行的预算约束。 |
| 记在谁名下 | **不确定** | 调用不带 task_id，不进任何 Task 的成本（已处理）；但 `TenantContext` 是被学习的那个租户（`attribution.py:79`），Model Gateway 的计量记在该租户名下（`user_id = learning-pipeline`）。设计书说后台任务“单独记预算”，没说算不算租户的账。以后租户有预算上限时，平台的后台任务会占用租户的额度。要定口径。 |

### 1.3 运行中的 Task 不受影响

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 已经固定的版本 | **已处理** | Learning 只经 Registry 的 Publish、Promote 写（细化第 2 节第 5 步）；Task 已固定的 Recipe、Node Template、租户策略按版本读（T-08）。 |
| 规划时可选的 Recipe 列表 | **未处理** | 真正的 Planner 每一轮的 Recipe 列表取的是**当前默认版本**：`RegistryContextSource.load` 调 `list_recipes(inp.tenant)`，不带 Task 的固定版本（`agentruntime_worker/registry/catalog.py:94`；Node Template 那一行带了 `inp.pinned`）。Learning 晋升一个新 Recipe 之后，运行中的 Task 在下一次重规划或 reroute 时就能选到它。根子是 T-08 登记到后续的“全局 Registry 修订号”（设计书 3.1.2，要改 proto）；在那之前，这一条对 Learning 的发布不成立。 |
| 灰度与回滚 | **未处理（已登记）** | J.3 末句“达标后灰度放量，指标变差就自动回滚”没有：Promote 之后立即成为该租户所有新 Task 的默认版本。细化第 2 节“不做”已登记。 |

### 1.4 失败归因提示

| 方面 | 结论 | 位置与说明 |
| --- | --- | --- |
| 三类与输出校验 | **已处理** | 提示里给出 MAST 三类的判别说明，要求只输出 JSON；输出按 schema 校验，不合格、调用失败、没有 Gateway、预算用完都记 unattributed（`attribution.py:92`）。只看 aborted、failed，取消的不算。 |
| 输入里有没有用户数据 | **不一致** | 模块说明写“trace 里本来就没有用户数据”（`attribution.py:5`），但提示里的执行计划是 `stage_shape`，含 Stage 的 goal、exit_when 与 Goal Node 的 goal、done_when（`verdicts.py:48`），这些是 Planner 写的，细化第 4 节第 4 条自己也说“可能带具体业务名词（例如客户名）”。调用仍在同一租户的上下文里，不算跨租户泄露，但说法要改，或者归因只给结构字段（id、kind、template、after、tier、mode）。 |
| 提示注入 | **风险（低）** | goal 文字间接来自用户消息，可以影响归因的类别；归因只进报告，不反哺任何配置，影响有限。等归因开始反哺路由与模板（J.2 后半句）时必须先处理。 |
| 归因的用处 | **未处理（已登记）** | J.2“归因结果反哺路由规则、Model Profile 阈值、Node Template 与错误类经验”第一期没有，只进报告。 |

## 2. 其他风险

### 2.1 确定性

- Pipeline 不是工作流代码，不受 Temporal 重放约束。候选、门槛与证据对同一批 trace 确定（排序、并列取字典序最小、证据版本是 task_id 集合的哈希）；
  测试覆盖“跑两次相同”“读的顺序不影响结果”。
- 失败重跑整条是安全的：门槛不通过不写；通过时 Publish 每次新建一个影子版本，重跑会多出影子版本，但默认版本只由 Promote 决定。没有发现确定性风险。

### 2.2 并发

- 同一任务族的两次运行并发时，两边各自 Publish、Promote，后一次的 Promote 覆盖前一次，结果取决于先后。第一期是一次性命令，CronJob/ScaledJob 要配成同一任务族不并发
  （`concurrencyPolicy: Forbid` 或按任务族加锁）。

### 2.3 租户隔离

- **已处理**：按 span 上的 `tenant_id` 过滤（`traces.py:148`）；这个属性由 Telemetry 绑定时加上，调用方传入的同名属性被覆盖（Telemetry 一致性测试 `conformance/telemetry.py:97`）；
  其他租户的 span 只计数；发布在 `--tenant` 的租户作用域；跨租户读取在平台作用域记审计 `learning.read_traces`。测试把另一个租户的大量 trace 混进去，结果逐字段不变。
- 风险：Learning 读的是 Collector 写出的全部租户的 trace，能读到这份文件或 Kafka topic 的进程就能看到所有租户的 Stage 形状（含 goal 文字）。这是部署层的访问控制，登记。

## 3. 与设计书不一致的地方

1. 每次成功成本只算成功 Task 的均值，不含失败（H.2、J.3）。见 1.1。
2. Task 成本不含 tool_loop、Planner、Reviewer（1.1“成本可控”、J.4 的 Recipe 统计口径）。与 R-01 评审 2.2 同源。
3. 运行中的 Task 重规划时看得到新晋升的 Recipe（J.5“运行中的 Task 不受影响”、3.1.2 的固定版本）。见 1.3。
4. 观测式评测代替影子评测、没有灰度与回滚、归因不反哺、不从 Redpanda 消费、不在 offline 队列上跑：细化第 2 节与第 4 节第 1 条已定、已登记。

## 4. 处理建议

**本期修**（小，只动 Learning 自己）：

1. 每次成功成本改为“组内全部 Task 的成本之和 ÷ 成功数”，上界用 delta 方法或按 Task 的自助法（bootstrap，固定种子保持确定）；改评测测试的期望值。
2. 归因提示只给结构字段与 verdict，去掉 goal、exit_when、done_when；或保留但改掉模块说明里“没有用户数据”的说法。建议前者：归因的三类靠结构与 verdict 序列就能判。
3. 部署：Learning 的 ScaledJob 同一任务族不并发（Chart 里加 `concurrencyPolicy` 或命令里按任务族加锁），并在细化里写明。

**要人定方案**：

4. Task 成本的口径（与 R-01 评审第 4 条是同一件事）：Round 的成本是否计入 tool_loop、Planner、Reviewer。不改的话，Learning 的成本门槛对 tool_loop 为主的任务族没有意义。
5. 规划时的 Recipe 列表按 Task 固定：要么先做全局 Registry 修订号（改 proto，走 S-01），要么 Task 开始时把可选 Recipe 的版本集合固定进 Task State 的 pinned
   （不改 proto，但改 Task 工作流的数据流，属于核心链路）。
6. Learning 调用的计量记在谁名下：平台账（`bind_platform`）还是被学习的租户的账。

**登记到后续**（写进 `docs/reference/risks-and-open-questions.md`）：Learning 自己的发布令牌与角色；跨运行的预算总额与按价值抽样；Gateway 执行 `max_cost_usd_micros`（R-02 已登记）；
trace 存储的访问控制；归因反哺之前先处理提示注入。

人定方案（2026-10-24）：第 1 到 3 条现在修；第 4 条 Task 成本全部计入（tool_loop 每一步、Planner、Reviewer、子工作流）；第 5、6 条登记。

## 5. 另一家复审（2026-10-24）

OpenAI gpt-6-sol 按 6.2 的模板复审，原文与材料包见 [reviews/R-06-gpt-6-sol.md](reviews/R-06-gpt-6-sol.md)。满足写审分离，R-06 算评审完成。

- **与本评审一致**：门槛先于发布、Registry 的第二道、样本下限、选择偏差、确定性、至少一次投递为“已处理”；单次调用超支取决于网关执行
  `max_cost_usd_micros`；同一任务族并发运行时 Promote 的先后决定默认版本。
- **新提出、采纳并在这一轮修**（都只动 Learning 自己）：
  1. 现行组的 Recipe：`_baseline_recipe` 把该任务族的默认 Recipe 排序后取第一个，同一任务族有几个默认 Recipe 时现行组是随意的一个。改为这些默认 Recipe 的 Task 的并集，报告里列出用了哪些。
  2. 失败归因的输出没有按完整 schema 校验（reason 非空、长度上限、不许多余字段）。改用 jsonschema 校验。
  3. 审计 `learning.read_traces` 没有记读了哪些文件（细化第 2 节第 2 步要求）。补上文件名。
  4. 报告文件名精确到秒，同一秒重跑会覆盖。改为精确到微秒。
- **新提出、登记**：重跑通过门槛时每次都 Publish 一个新的影子版本（默认版本只由 Promote 决定，结果正确但多出影子版本）；Registry 的 Promote 没有
  “基于哪个现行版本”的比较（CAS）；Chart 里 Learning 的 ScaledJob 按 Kafka 积压触发、Job 却读固定的 trace 文件（第一期只能读文件，触发器的语义要等 EventStream 的 Kafka 适配器）。
- **判“不确定”、已核对**：span 上的 `tenant_id` 由 Telemetry 绑定时加上，调用方传入的同名属性被覆盖，由 Telemetry 的一致性测试
  （`agentruntime_core/conformance/telemetry.py`）在控制台与 OpenTelemetry 两个实现上验证；运行中的 Task 固定版本由 T-08 的 `tests/e2e/test_registry.py` 验证，
  但规划时可选的 Recipe 列表不按固定版本（本评审 1.3），与复审的“不确定”相符。
- **不同意**：复审认为“每次成功成本”按 J.3 取成功 Task 的均值即可。J.3 说的“每次成功成本”与 H.2“每次成功的期望成本”、S-08 评测报告的 `per_success_usd`
  是同一个量（总成本 ÷ 成功数），失败的花费要算进去，维持本评审 1.1 的结论并修掉。
